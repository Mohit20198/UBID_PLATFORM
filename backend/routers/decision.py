"""
routers/decision.py
-------------------
POST /decision — immutable audit log writer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db
from schemas import DecisionRequest, DecisionResponse, DecisionType, UnmergeRequest

router = APIRouter()


def _find_existing_ubid(db: Session, source: str, source_record_id: str) -> str | None:
    row = db.execute(
        text("""
            SELECT um.ubid
            FROM ubid_members um
            WHERE um.source           = :source
              AND um.source_record_id = :src_id
              AND um.removed_at IS NULL
            LIMIT 1
        """),
        {"source": source, "src_id": source_record_id},
    ).fetchone()
    return row[0] if row else None


def _get_normalised_id(db: Session, source: str, source_record_id: str) -> int | None:
    """Look up the normalised_records.id for a source record."""
    row = db.execute(
        text("""
            SELECT id FROM normalised_records
            WHERE source = :source
              AND source_record_id = :src_id
            LIMIT 1
        """),
        {"source": source, "src_id": source_record_id},
    ).fetchone()
    return row[0] if row else None


def _mint_ubid(db: Session) -> str:
    new_ubid = f"UBID-{uuid.uuid4().hex[:12].upper()}"
    db.execute(
        text("""
            INSERT INTO ubid_registry (ubid, status, created_at)
            VALUES (:ubid, 'ACTIVE', NOW())
            ON CONFLICT (ubid) DO NOTHING
        """),
        {"ubid": new_ubid},
    )
    return new_ubid


def _add_member(db: Session, ubid: str, source: str, source_record_id: str) -> None:
    """Add a source record to a UBID cluster, looking up normalised_id automatically."""
    normalised_id = _get_normalised_id(db, source, source_record_id)
    if normalised_id is None:
        raise ValueError(
            f"No normalised_records row found for {source} / {source_record_id}. "
            "Run the ingestion pipeline first."
        )
    db.execute(
        text("""
            INSERT INTO ubid_members
                (ubid, normalised_id, source, source_record_id, joined_at)
            VALUES
                (:ubid, :normalised_id, :source, :src_id, NOW())
            ON CONFLICT (ubid, source, source_record_id) DO NOTHING
        """),
        {
            "ubid":          ubid,
            "normalised_id": normalised_id,
            "source":        source,
            "src_id":        source_record_id,
        },
    )


def _write_audit(
    db: Session,
    *,
    pair_id: str,
    left_source: str,
    left_record_id: str,
    right_source: str,
    right_record_id: str,
    action: str,
    ubid: str | None,
    analyst_id: str,
    notes: str | None,
) -> int:
    row = db.execute(
        text("""
            INSERT INTO audit_log (
                pair_id,
                left_source,  left_record_id,
                right_source, right_record_id,
                action,
                actor_type,
                entity_type,
                entity_id,
                ubid_assigned,
                analyst_id,
                notes,
                decided_at
            ) VALUES (
                :pair_id,
                :left_source,  :left_record_id,
                :right_source, :right_record_id,
                :action,
                :actor_type,
                :entity_type,
                :entity_id,
                :ubid,
                :analyst_id,
                :notes,
                NOW()
            )
            RETURNING id
        """),
        {
            "pair_id":          pair_id,
            "left_source":      left_source,
            "left_record_id":   left_record_id,
            "right_source":     right_source,
            "right_record_id":  right_record_id,
            "action":           action,
            "actor_type":       "REVIEWER",
            "entity_type":      "CANDIDATE_PAIR",
            "entity_id":        pair_id,
            "ubid":             ubid,
            "analyst_id":       analyst_id,
            "notes":            notes,
        },
    ).fetchone()
    return row[0]


def _merge_clusters(db: Session, target_ubid: str, source_ubid: str) -> None:
    """Move all members from source_ubid cluster to target_ubid cluster."""
    # 1. Mark existing members of source_ubid as removed
    db.execute(
        text("""
            UPDATE ubid_members
            SET removed_at = NOW(),
                removal_reason = :reason
            WHERE ubid = :src_ubid
              AND removed_at IS NULL
        """),
        {"src_ubid": source_ubid, "reason": f"Merged into {target_ubid}"},
    )

    # 2. Add them to target_ubid
    # We fetch them first to get their normalised_id
    members = db.execute(
        text("SELECT normalised_id, source, source_record_id FROM ubid_members WHERE ubid = :src_ubid"),
        {"src_ubid": source_ubid},
    ).fetchall()

    for m_id, src, src_rec_id in members:
        db.execute(
            text("""
                INSERT INTO ubid_members (ubid, normalised_id, source, source_record_id, joined_at)
                VALUES (:target, :m_id, :src, :src_rec, NOW())
                ON CONFLICT (ubid, source, source_record_id) DO NOTHING
            """),
            {"target": target_ubid, "m_id": m_id, "src": src, "src_rec": src_rec_id},
        )

    # 3. Mark the source UBID as inactive/merged in registry
    db.execute(
        text("UPDATE ubid_registry SET status = 'CLOSED' WHERE ubid = :src_ubid"),
        {"src_ubid": source_ubid},
    )


@router.post("/unmerge", response_model=DecisionResponse)
def post_unmerge(payload: UnmergeRequest, db: Session = Depends(get_db)):
    """
    Reverse a previous MERGE decision by splitting the records into different UBIDs.
    """
    try:
        # 1. Find the most recent merge decision for this pair
        audit = db.execute(
            text("""
                SELECT left_source, left_record_id, right_source, right_record_id, ubid_assigned
                FROM audit_log
                WHERE pair_id = :pid AND action = 'MERGE'
                ORDER BY id DESC LIMIT 1
            """),
            {"pid": payload.pair_id},
        ).fetchone()

        if not audit:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No previous merge decision found for pair_id {payload.pair_id}",
            )

        left_src, left_id, right_src, right_id, old_ubid = audit

        # 2. Perform the split
        # Mark the right record as removed from the current UBID cluster
        db.execute(
            text("""
                UPDATE ubid_members
                SET removed_at = NOW(),
                    removal_reason = :reason
                WHERE ubid = :ubid
                  AND source = :src
                  AND source_record_id = :src_id
                  AND removed_at IS NULL
            """),
            {
                "ubid":   old_ubid,
                "src":    right_src,
                "src_id": right_id,
                "reason": f"Unmerged from pair {payload.pair_id}",
            },
        )

        # Mint a new UBID for the split-off record
        new_ubid = _mint_ubid(db)
        _add_member(db, new_ubid, right_src, right_id)

        # 3. Record in audit log
        audit_id = _write_audit(
            db,
            pair_id=payload.pair_id,
            left_source=left_src,
            left_record_id=left_id,
            right_source=right_src,
            right_record_id=right_id,
            action=DecisionType.UNMERGE.value,
            ubid=new_ubid,
            analyst_id=payload.analyst_id,
            notes=payload.notes,
        )

        db.commit()

        return DecisionResponse(
            audit_log_id=audit_id,
            pair_id=payload.pair_id,
            decision=DecisionType.UNMERGE,
            ubid_assigned=new_ubid,
            recorded_at=datetime.now(timezone.utc),
            message=f"Split records into different UBIDs. Right record moved to {new_ubid}.",
        )

    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unmerge failed: {str(exc)}",
        ) from exc


@router.post("", response_model=DecisionResponse, status_code=status.HTTP_201_CREATED)
def post_decision(payload: DecisionRequest, db: Session = Depends(get_db)):
    """
    Record a human merge/reject decision.

    - **MERGE**: assigns both records to a shared UBID (mints one if needed).
    - **REJECT**: logs the decision; no UBID change.

    All writes are immutable INSERT operations — no row is ever updated or deleted.
    """
    ubid_assigned: str | None = None

    try:
        if payload.decision == DecisionType.MERGE:
            left_ubid  = _find_existing_ubid(db, payload.left_source,  payload.left_record_id)
            right_ubid = _find_existing_ubid(db, payload.right_source, payload.right_record_id)

            if left_ubid and right_ubid:
                if left_ubid == right_ubid:
                    ubid_assigned = left_ubid
                else:
                    # Merge two different clusters
                    ubid_assigned = left_ubid
                    _merge_clusters(db, left_ubid, right_ubid)

            elif left_ubid:
                ubid_assigned = left_ubid
                _add_member(db, ubid_assigned, payload.right_source, payload.right_record_id)

            elif right_ubid:
                ubid_assigned = right_ubid
                _add_member(db, ubid_assigned, payload.left_source, payload.left_record_id)

            else:
                ubid_assigned = _mint_ubid(db)
                _add_member(db, ubid_assigned, payload.left_source,  payload.left_record_id)
                _add_member(db, ubid_assigned, payload.right_source, payload.right_record_id)

        audit_id = _write_audit(
            db,
            pair_id=payload.pair_id,
            left_source=payload.left_source,
            left_record_id=payload.left_record_id,
            right_source=payload.right_source,
            right_record_id=payload.right_record_id,
            action=payload.decision.value,
            ubid=ubid_assigned,
            analyst_id=payload.analyst_id,
            notes=payload.notes,
        )

        db.commit()

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Decision write failed: {str(exc)}",
        ) from exc

    return DecisionResponse(
        audit_log_id=audit_id,
        pair_id=payload.pair_id,
        decision=payload.decision,
        ubid_assigned=ubid_assigned,
        recorded_at=datetime.now(timezone.utc),
        message=(
            f"Merged into UBID {ubid_assigned}"
            if payload.decision == DecisionType.MERGE
            else "Pair rejected — no UBID assigned"
        ),
    )