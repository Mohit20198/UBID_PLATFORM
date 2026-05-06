"""
routers/review.py
-----------------
GET /review-queue

Returns paginated candidate pairs for human review.

Strategy:
  1. Pull normalised_records cross-joined within the same pin_code block
     (blocking key = addr_pin_code) — this is the coarse blocking step.
  2. Score each pair with similarity.py.
  3. Filter out pairs already decided (present in audit_log).
  4. Sort by similarity_score DESC (hardest cases first for demo purposes
     — in production you'd flip this to show HIGH-confidence first).
  5. Return paginated JSON.

For the demo dataset (~770 records across 2 pin codes) this runs in < 200 ms.
Day 3 will add a pre-computed candidate_pairs table for larger datasets.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db
from schemas import (
    CandidatePair, RecordSnippet, ReviewQueueResponse, ConfidenceBand,
)
from similarity import compute_similarity, make_pair_id, confidence_band

router = APIRouter()


# ─────────────────────────────────────────────────────────────
# Helper: fetch all normalised records for a pin code block
# ─────────────────────────────────────────────────────────────

_RECORD_COLS = """
    id, source, source_record_id,
    name_original, name_normalised, name_tokens, name_soundex,
    addr_full_normalised, addr_pin_code,
    pan, pan_valid, gstin, gstin_valid, gstin_prefix,
    phone_normalised, email_normalised,
    sector, registration_year, identifier_issues
"""

def _fetch_records_by_pin(db: Session, pin_code: Optional[str]) -> list[dict]:
    if pin_code:
        rows = db.execute(
            text(f"SELECT {_RECORD_COLS} FROM normalised_records WHERE addr_pin_code = :pin"),
            {"pin": pin_code},
        ).mappings().all()
    else:
        rows = db.execute(
            text(f"SELECT {_RECORD_COLS} FROM normalised_records"),
        ).mappings().all()
    return [dict(r) for r in rows]


def _fetch_decided_pairs(db: Session) -> set[str]:
    """Return the set of pair_ids already in the audit log."""
    rows = db.execute(
        text("SELECT pair_id FROM audit_log WHERE pair_id IS NOT NULL")
    ).fetchall()
    return {r[0] for r in rows}


def _to_snippet(r: dict) -> RecordSnippet:
    return RecordSnippet(
        source=r["source"],
        source_record_id=r["source_record_id"],
        name_original=r.get("name_original"),
        name_normalised=r.get("name_normalised"),
        addr_pin_code=r.get("addr_pin_code"),
        addr_full_normalised=r.get("addr_full_normalised"),
        pan=r.get("pan"),
        gstin=r.get("gstin"),
        sector=r.get("sector"),
        registration_year=r.get("registration_year"),
        identifier_issues=r.get("identifier_issues") or [],
    )


# ─────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────

@router.get("", response_model=ReviewQueueResponse)
def get_review_queue(
    pin_code:         Optional[str] = Query(None,  description="Filter to a single pin code block"),
    min_score:        float         = Query(0.40,  ge=0.0, le=1.0, description="Minimum similarity threshold"),
    confidence:       Optional[str] = Query(None,  description="Filter by band: HIGH, MEDIUM, LOW"),
    include_decided:  bool          = Query(False, description="Include pairs already in audit_log"),
    page:             int           = Query(1,     ge=1),
    page_size:        int           = Query(20,    ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Returns candidate entity pairs for human review.

    - Blocking: pairs only within the same `pin_code` (pass pin_code param to restrict further).
    - Scoring: weighted similarity across name tokens, PAN, GSTIN prefix, address.
    - Excludes cross-source same-entity pairs that already have an audit decision
      (unless include_decided=true).
    """
    records = _fetch_records_by_pin(db, pin_code)
    decided = _fetch_decided_pairs(db) if not include_decided else set()

    # ── Generate candidate pairs (cross-product within pin-code blocks) ──
    # Group records by pin_code for blocking
    from collections import defaultdict
    pin_groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        pin_groups[r.get("addr_pin_code") or "unknown"].append(r)

    all_pairs: list[CandidatePair] = []

    for pin, group in pin_groups.items():
        # Only pair records from DIFFERENT sources (same source = different business)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left  = group[i]
                right = group[j]

                # Skip same-source pairs
                if left["source"] == right["source"]:
                    continue

                score, signals = compute_similarity(left, right)

                if score < min_score:
                    continue

                band = confidence_band(score)
                if confidence and band != confidence.upper():
                    continue

                pid = make_pair_id(
                    left["source"], left["source_record_id"],
                    right["source"], right["source_record_id"],
                )

                already = pid in decided
                if already and not include_decided:
                    continue

                # Look up existing decision label if needed
                existing_dec = None
                if already:
                    row = db.execute(
                        text("SELECT decision FROM audit_log WHERE pair_id = :pid ORDER BY id DESC LIMIT 1"),
                        {"pid": pid},
                    ).fetchone()
                    existing_dec = row[0] if row else None

                all_pairs.append(CandidatePair(
                    pair_id=pid,
                    left=_to_snippet(left),
                    right=_to_snippet(right),
                    similarity_score=score,
                    confidence_band=ConfidenceBand(band),
                    match_signals=signals,
                    already_decided=already,
                    existing_decision=existing_dec,
                ))

    # Sort: undecided HIGH-confidence first, then by score DESC
    all_pairs.sort(key=lambda p: (p.already_decided, -p.similarity_score))

    total = len(all_pairs)
    start = (page - 1) * page_size
    end   = start + page_size

    return ReviewQueueResponse(
        total_pending=total,
        page=page,
        page_size=page_size,
        pairs=all_pairs[start:end],
    )
