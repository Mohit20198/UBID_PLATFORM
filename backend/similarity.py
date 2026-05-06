"""
similarity.py
-------------
Lightweight entity similarity scorer.

Used by the review-queue endpoint to compute match signals between
two normalised_records rows without pulling in a heavy ML library.

Signals (each 0.0–1.0):
  - name_token_jaccard   : Jaccard similarity of sorted name token sets
  - name_soundex_match   : whether leading soundex codes match
  - pan_exact            : PAN match (both present and equal)
  - gstin_prefix_match   : first 12 chars of GSTIN match
  - pin_code_match       : pin codes match
  - address_token_jaccard: Jaccard of address tokens

Final score: weighted sum, clamped to [0, 1].
"""

from __future__ import annotations

import hashlib
from typing import Any

# Weight table — tuned for Karnataka regulatory data
_WEIGHTS = {
    "pan_exact":             0.35,
    "gstin_prefix_match":    0.25,
    "name_token_jaccard":    0.20,
    "pin_code_match":        0.10,
    "name_soundex_match":    0.05,
    "address_token_jaccard": 0.05,
}


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def compute_similarity(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """
    Returns (overall_score, signal_breakdown).

    Both `left` and `right` are rows from normalised_records as plain dicts.
    """
    signals: dict[str, float] = {}

    # ── PAN exact match ──────────────────────────────────────
    l_pan = left.get("pan")
    r_pan = right.get("pan")
    if l_pan and r_pan and left.get("pan_valid") and right.get("pan_valid"):
        signals["pan_exact"] = 1.0 if l_pan == r_pan else 0.0
    else:
        signals["pan_exact"] = 0.0

    # ── GSTIN prefix (first 12 chars) ────────────────────────
    l_g = left.get("gstin_prefix")
    r_g = right.get("gstin_prefix")
    if l_g and r_g:
        signals["gstin_prefix_match"] = 1.0 if l_g == r_g else 0.0
    else:
        signals["gstin_prefix_match"] = 0.0

    # ── Name token Jaccard ───────────────────────────────────
    signals["name_token_jaccard"] = _jaccard(
        left.get("name_tokens") or [],
        right.get("name_tokens") or [],
    )

    # ── Pin code match ───────────────────────────────────────
    l_pin = left.get("addr_pin_code")
    r_pin = right.get("addr_pin_code")
    signals["pin_code_match"] = 1.0 if (l_pin and r_pin and l_pin == r_pin) else 0.0

    # ── Soundex match (leading token) ────────────────────────
    l_sx = left.get("name_soundex") or []
    r_sx = right.get("name_soundex") or []
    if l_sx and r_sx:
        signals["name_soundex_match"] = 1.0 if l_sx[0] == r_sx[0] else 0.0
    else:
        signals["name_soundex_match"] = 0.0

    # ── Address token Jaccard ────────────────────────────────
    l_addr = (left.get("addr_full_normalised") or "").split()
    r_addr = (right.get("addr_full_normalised") or "").split()
    signals["address_token_jaccard"] = _jaccard(l_addr, r_addr)

    # ── Weighted sum ─────────────────────────────────────────
    score = sum(_WEIGHTS[k] * v for k, v in signals.items())
    score = round(min(max(score, 0.0), 1.0), 4)

    return score, signals


def make_pair_id(left_source: str, left_id: str, right_source: str, right_id: str) -> str:
    """Stable, order-independent hash for a candidate pair."""
    key = "_".join(sorted([f"{left_source}:{left_id}", f"{right_source}:{right_id}"]))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def confidence_band(score: float) -> str:
    if score >= 0.85:
        return "HIGH"
    if score >= 0.60:
        return "MEDIUM"
    return "LOW"
