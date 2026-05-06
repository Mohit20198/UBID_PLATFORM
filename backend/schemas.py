"""
schemas.py
----------
Pydantic v2 request/response models for all Day 2 endpoints.
These are the API contracts — separate from the DB ORM models.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────
# Shared enums
# ─────────────────────────────────────────────────────────────

class DecisionType(str, Enum):
    MERGE   = "MERGE"
    REJECT  = "REJECT"
    UNMERGE = "UNMERGE"


class ConfidenceBand(str, Enum):
    HIGH   = "HIGH"    # ≥ 0.85  — auto-mergeable
    MEDIUM = "MEDIUM"  # 0.60–0.84 — human review
    LOW    = "LOW"     # < 0.60  — likely different entities


# ─────────────────────────────────────────────────────────────
# GET /review-queue
# ─────────────────────────────────────────────────────────────

class RecordSnippet(BaseModel):
    """Compact representation of one source record shown in the UI."""
    source:            str
    source_record_id:  str
    name_original:     Optional[str]
    name_normalised:   Optional[str]
    addr_pin_code:     Optional[str]
    addr_full_normalised: Optional[str]
    pan:               Optional[str]
    gstin:             Optional[str]
    sector:            Optional[str]
    registration_year: Optional[int]
    identifier_issues: list[str] = []


class CandidatePair(BaseModel):
    """One candidate pair returned by the review queue."""
    pair_id:          str = Field(..., description="Stable hash of (left_id, right_id)")
    left:             RecordSnippet
    right:            RecordSnippet
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    confidence_band:  ConfidenceBand
    match_signals:    dict[str, Any] = Field(
        default_factory=dict,
        description="Breakdown of individual similarity signals (name, address, pan, gstin, …)",
    )
    already_decided:  bool = False
    existing_decision: Optional[str] = None   # MERGE / REJECT if already decided


class ReviewQueueResponse(BaseModel):
    total_pending:  int
    page:           int
    page_size:      int
    pairs:          list[CandidatePair]


# ─────────────────────────────────────────────────────────────
# POST /decision
# ─────────────────────────────────────────────────────────────

class DecisionRequest(BaseModel):
    """Payload sent by the UI when an analyst clicks Merge or Reject."""
    pair_id:         str   = Field(..., description="Must match a pair_id from /review-queue")
    left_record_id:  str   = Field(..., description="source_record_id of the left record")
    right_record_id: str   = Field(..., description="source_record_id of the right record")
    left_source:     str
    right_source:    str
    decision:        DecisionType
    analyst_id:      str   = Field(default="anonymous", description="Analyst or system making the decision")
    notes:           Optional[str] = Field(None, max_length=1000)

    @field_validator("pair_id")
    @classmethod
    def pair_id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("pair_id cannot be blank")
        return v.strip()


class DecisionResponse(BaseModel):
    """Returned after a decision is written to the audit log."""
    audit_log_id:   int
    pair_id:        str
    decision:       DecisionType
    ubid_assigned:  Optional[str] = Field(
        None,
        description="New or existing UBID if decision=MERGE, else null",
    )
    recorded_at:    datetime
    message:        str


class UnmergeRequest(BaseModel):
    """Payload to reverse a previous merge decision."""
    pair_id:    str
    analyst_id: str = "anonymous"
    notes:      Optional[str] = None


# ─────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status:   str
    db_ok:    bool
    version:  str = "0.2.0"
