"""
pydantic_contracts.py
---------------------
Strict Pydantic v2 models for each of the 4 synthetic department systems.
These are the schema contracts at the ingestion boundary.
A record that fails validation is logged and quarantined — never silently dropped.

Usage:
    from pydantic_contracts import BBMPRecord, ESCOMRecord, LabourRecord, FactoriesRecord
    from pydantic_contracts import RawIngestRecord, parse_raw_record
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    ConfigDict,
)


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class DepartmentSource(str, Enum):
    BBMP         = "BBMP"
    ESCOM        = "ESCOM"
    LABOUR       = "LABOUR"
    POLLUTION    = "POLLUTION"
    GST          = "GST"
    BWSSB        = "BWSSB"
    FACTORIES    = "FACTORIES"
    COMMERCIAL_TAX = "COMMERCIAL_TAX"


class BusinessType(str, Enum):
    PROPRIETORSHIP  = "PROPRIETORSHIP"
    PARTNERSHIP     = "PARTNERSHIP"
    PRIVATE_LIMITED = "PRIVATE_LIMITED"
    PUBLIC_LIMITED  = "PUBLIC_LIMITED"
    LLP             = "LLP"
    HUF             = "HUF"
    TRUST           = "TRUST"
    SOCIETY         = "SOCIETY"
    OTHER           = "OTHER"
    UNKNOWN         = "UNKNOWN"


# ─────────────────────────────────────────────────────────────
# Reusable validators
# ─────────────────────────────────────────────────────────────

_PIN_RE     = re.compile(r"^\d{6}$")
_PAN_RE     = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_GSTIN_RE   = re.compile(r"^\d{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PHONE_RE   = re.compile(r"^[6-9]\d{9}$")


def _clean_str(v: Optional[str]) -> Optional[str]:
    """Strip whitespace; return None for empty strings."""
    if v is None:
        return None
    cleaned = v.strip()
    return cleaned if cleaned else None


def _validate_pan(pan: Optional[str]) -> tuple[Optional[str], bool]:
    """Returns (normalised_pan_or_None, is_valid)."""
    if not pan:
        return None, False
    pan = pan.strip().upper().replace(" ", "")
    return pan, bool(_PAN_RE.match(pan))


def _validate_gstin(gstin: Optional[str]) -> tuple[Optional[str], bool]:
    """Returns (normalised_gstin_or_None, is_valid)."""
    if not gstin:
        return None, False
    gstin = gstin.strip().upper().replace(" ", "")
    return gstin, bool(_GSTIN_RE.match(gstin))


def _validate_pin(pin: Optional[str]) -> Optional[str]:
    if not pin:
        return None
    pin = pin.strip()
    return pin if _PIN_RE.match(pin) else None


# ─────────────────────────────────────────────────────────────
# Department 1: BBMP (Trade Licence records)
# ─────────────────────────────────────────────────────────────

class BBMPRecord(BaseModel):
    """
    BBMP Trade Licence database.
    Source fields as they arrive from the department system.
    All fields optional except the source record ID.
    """
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    trade_licence_no:   str   = Field(..., description="BBMP native primary key")
    business_name:      Optional[str] = None
    proprietor_name:    Optional[str] = None
    trade_category:     Optional[str] = None      # e.g. "FOOD", "GARMENT", "FACTORY"
    business_address:   Optional[str] = None
    ward_name:          Optional[str] = None
    ward_no:            Optional[str] = None
    pin_code:           Optional[str] = None
    pan:                Optional[str] = None
    gstin:              Optional[str] = None
    mobile:             Optional[str] = None
    email:              Optional[str] = None
    licence_issue_date: Optional[date] = None
    licence_expiry_date:Optional[date] = None
    licence_status:     Optional[str] = None      # ACTIVE / SUSPENDED / CANCELLED
    num_employees:      Optional[int] = Field(None, ge=0, le=100_000)
    annual_turnover:    Optional[float] = Field(None, ge=0)

    # Validation annotations (populated by validators)
    pan_valid:          Optional[bool] = Field(None, exclude=True)
    gstin_valid:        Optional[bool] = Field(None, exclude=True)
    pin_valid:          Optional[bool] = Field(None, exclude=True)
    validation_issues:  list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def run_identifier_checks(self) -> "BBMPRecord":
        issues: list[str] = []

        self.pan, self.pan_valid = _validate_pan(self.pan)
        if self.pan and not self.pan_valid:
            issues.append("PAN_FORMAT_INVALID")

        self.gstin, self.gstin_valid = _validate_gstin(self.gstin)
        if self.gstin and not self.gstin_valid:
            issues.append("GSTIN_FORMAT_INVALID")

        # Cross-check: PAN embedded in GSTIN (chars 3-12) should match
        if self.pan_valid and self.gstin_valid:
            if self.gstin[2:12] != self.pan:
                issues.append("GSTIN_PAN_MISMATCH")

        if self.pin_code:
            self.pin_code = self.pin_code.strip()
            self.pin_valid = bool(_PIN_RE.match(self.pin_code))
            if not self.pin_valid:
                issues.append("PIN_FORMAT_INVALID")

        if self.mobile:
            self.mobile = re.sub(r"[^\d]", "", self.mobile)[-10:]
            if not _PHONE_RE.match(self.mobile):
                issues.append("MOBILE_FORMAT_INVALID")

        self.validation_issues = issues
        return self


# ─────────────────────────────────────────────────────────────
# Department 2: ESCOM (Electricity consumer records)
# ─────────────────────────────────────────────────────────────

class ESCOMRecord(BaseModel):
    """
    ESCOM / BESCOM electricity consumer database.
    Commercial/industrial connections that map to businesses.
    """
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    consumer_no:        str   = Field(..., description="EB consumer number, native PK")
    consumer_name:      Optional[str] = None
    service_address:    Optional[str] = None
    pin_code:           Optional[str] = None
    connection_type:    Optional[str] = None      # HT / LT / COMMERCIAL / INDUSTRIAL
    sanctioned_load_kw: Optional[float] = Field(None, ge=0)
    connected_load_kw:  Optional[float] = Field(None, ge=0)
    tariff_category:    Optional[str] = None
    pan:                Optional[str] = None
    gstin:              Optional[str] = None
    connection_date:    Optional[date] = None
    connection_status:  Optional[str] = None      # ACTIVE / DISCONNECTED / SURRENDERED
    meter_no:           Optional[str] = None
    mobile:             Optional[str] = None

    pan_valid:          Optional[bool] = Field(None, exclude=True)
    gstin_valid:        Optional[bool] = Field(None, exclude=True)
    validation_issues:  list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def run_identifier_checks(self) -> "ESCOMRecord":
        issues: list[str] = []
        self.pan, self.pan_valid = _validate_pan(self.pan)
        if self.pan and not self.pan_valid:
            issues.append("PAN_FORMAT_INVALID")
        self.gstin, self.gstin_valid = _validate_gstin(self.gstin)
        if self.gstin and not self.gstin_valid:
            issues.append("GSTIN_FORMAT_INVALID")
        if self.pin_code:
            self.pin_code = self.pin_code.strip()
            if not _PIN_RE.match(self.pin_code):
                issues.append("PIN_FORMAT_INVALID")
        self.validation_issues = issues
        return self


# ─────────────────────────────────────────────────────────────
# Department 3: Labour Department (PF / ESI registrations)
# ─────────────────────────────────────────────────────────────

class LabourRecord(BaseModel):
    """
    Karnataka Labour Department — PF and ESI establishment records.
    One record per registered establishment.
    """
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    pf_code:            str   = Field(..., description="PF establishment code, native PK")
    establishment_name: Optional[str] = None
    employer_name:      Optional[str] = None
    industry_class:     Optional[str] = None      # NIC code or description
    address:            Optional[str] = None
    district:           Optional[str] = None
    pin_code:           Optional[str] = None
    pan:                Optional[str] = None
    gstin:              Optional[str] = None
    esi_code:           Optional[str] = None
    coverage_date:      Optional[date] = None     # date PF coverage began
    num_employees:      Optional[int] = Field(None, ge=0, le=500_000)
    wages_month:        Optional[float] = Field(None, ge=0)
    mobile:             Optional[str] = None
    email:              Optional[str] = None
    nic_code:           Optional[str] = None      # National Industry Classification

    pan_valid:          Optional[bool] = Field(None, exclude=True)
    gstin_valid:        Optional[bool] = Field(None, exclude=True)
    validation_issues:  list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def run_identifier_checks(self) -> "LabourRecord":
        issues: list[str] = []
        self.pan, self.pan_valid = _validate_pan(self.pan)
        if self.pan and not self.pan_valid:
            issues.append("PAN_FORMAT_INVALID")
        self.gstin, self.gstin_valid = _validate_gstin(self.gstin)
        if self.gstin and not self.gstin_valid:
            issues.append("GSTIN_FORMAT_INVALID")
        if self.pin_code:
            self.pin_code = self.pin_code.strip()
            if not _PIN_RE.match(self.pin_code):
                issues.append("PIN_FORMAT_INVALID")
        if self.num_employees and self.num_employees < 20:
            issues.append("PF_THRESHOLD_WARNING")  # PF mandatory above 20
        self.validation_issues = issues
        return self


# ─────────────────────────────────────────────────────────────
# Department 4: Factories Inspectorate
# ─────────────────────────────────────────────────────────────

class FactoriesRecord(BaseModel):
    """
    Karnataka Factories Inspectorate — factory licence records.
    Section 2(m) of Factories Act — premises with 10+ workers with power,
    or 20+ without power.
    """
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    factory_licence_no:         str   = Field(..., description="Factory licence, native PK")
    factory_name:               Optional[str] = None
    occupier_name:              Optional[str] = None
    manager_name:               Optional[str] = None
    product_description:        Optional[str] = None
    factory_address:            Optional[str] = None
    pin_code:                   Optional[str] = None
    district:                   Optional[str] = None
    pan:                        Optional[str] = None
    gstin:                      Optional[str] = None
    cin:                        Optional[str] = None      # Corporate Identity Number
    licence_issue_date:         Optional[date] = None
    licence_valid_upto:         Optional[date] = None
    licence_status:             Optional[str] = None
    num_workers:                Optional[int] = Field(None, ge=0)
    power_used:                 Optional[bool] = None
    installed_capacity_hp:      Optional[float] = Field(None, ge=0)
    nic_code:                   Optional[str] = None
    last_inspection_date:       Optional[date] = None
    inspection_result:          Optional[str] = None      # SATISFACTORY / NOTICE_ISSUED / etc.
    mobile:                     Optional[str] = None

    pan_valid:          Optional[bool] = Field(None, exclude=True)
    gstin_valid:        Optional[bool] = Field(None, exclude=True)
    validation_issues:  list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def run_identifier_checks(self) -> "FactoriesRecord":
        issues: list[str] = []
        self.pan, self.pan_valid = _validate_pan(self.pan)
        if self.pan and not self.pan_valid:
            issues.append("PAN_FORMAT_INVALID")
        self.gstin, self.gstin_valid = _validate_gstin(self.gstin)
        if self.gstin and not self.gstin_valid:
            issues.append("GSTIN_FORMAT_INVALID")
        if self.pin_code:
            self.pin_code = self.pin_code.strip()
            if not _PIN_RE.match(self.pin_code):
                issues.append("PIN_FORMAT_INVALID")
        if self.num_workers is not None:
            if self.power_used and self.num_workers < 10:
                issues.append("FACTORY_ACT_THRESHOLD_WARNING")
            elif not self.power_used and self.num_workers < 20:
                issues.append("FACTORY_ACT_THRESHOLD_WARNING")
        self.validation_issues = issues
        return self


# ─────────────────────────────────────────────────────────────
# Generic wrapper for the raw_ingest table
# ─────────────────────────────────────────────────────────────

class RawIngestRecord(BaseModel):
    """Envelope written to raw_ingest for every successfully parsed source record."""
    source:             DepartmentSource
    source_record_id:   str
    raw_payload:        dict[str, Any]
    ingest_batch_id:    UUID
    is_scrambled:       bool = True


class IngestFailure(BaseModel):
    """Written to a quarantine table when a record fails schema validation."""
    source:             DepartmentSource
    source_record_id:   Optional[str] = None
    raw_row:            dict[str, Any]
    ingest_batch_id:    UUID
    error_details:      str
    failed_at:          datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
# Factory function: parse any source record
# ─────────────────────────────────────────────────────────────

_MODEL_MAP: dict[DepartmentSource, type] = {
    DepartmentSource.BBMP:      BBMPRecord,
    DepartmentSource.ESCOM:     ESCOMRecord,
    DepartmentSource.LABOUR:    LabourRecord,
    DepartmentSource.FACTORIES: FactoriesRecord,
}

_SOURCE_PK_MAP: dict[DepartmentSource, str] = {
    DepartmentSource.BBMP:      "trade_licence_no",
    DepartmentSource.ESCOM:     "consumer_no",
    DepartmentSource.LABOUR:    "pf_code",
    DepartmentSource.FACTORIES: "factory_licence_no",
}


def parse_raw_record(
    source: DepartmentSource,
    row: dict[str, Any],
    batch_id: UUID,
) -> tuple[RawIngestRecord | None, IngestFailure | None]:
    """
    Parse a raw dict from a department system into a typed model.

    Returns:
        (RawIngestRecord, None)  — on success
        (None, IngestFailure)    — on validation failure
    """
    model_cls = _MODEL_MAP.get(source)
    pk_field  = _SOURCE_PK_MAP.get(source)

    if model_cls is None:
        return None, IngestFailure(
            source=source,
            raw_row=row,
            ingest_batch_id=batch_id,
            error_details=f"No Pydantic model registered for source {source}",
        )

    try:
        parsed = model_cls.model_validate(row)
        source_record_id = getattr(parsed, pk_field)
        return RawIngestRecord(
            source=source,
            source_record_id=str(source_record_id),
            raw_payload=row,
            ingest_batch_id=batch_id,
        ), None

    except Exception as exc:
        return None, IngestFailure(
            source=source,
            source_record_id=row.get(pk_field),
            raw_row=row,
            ingest_batch_id=batch_id,
            error_details=str(exc),
        )
