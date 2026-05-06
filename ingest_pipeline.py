"""
ingest_pipeline.py
------------------
Day 1 core deliverable: reads the 4 synthetic CSV files, validates each
record through Pydantic contracts, normalises to canonical form, and
loads into PostgreSQL.

This script is the manual/one-shot version. The Airflow DAG will wrap it.

Usage:
    python ingest_pipeline.py --data-dir ./synthetic_data --db-url postgresql://...

Environment variables (override CLI):
    DATABASE_URL   - PostgreSQL connection string
    DATA_DIR       - path to synthetic_data directory
    BATCH_ID       - UUID for this ingest run (auto-generated if absent)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values

# Local modules
sys.path.insert(0, str(Path(__file__).parent))
from pydantic_contracts import (
    DepartmentSource, parse_raw_record,
    BBMPRecord, ESCOMRecord, LabourRecord, FactoriesRecord,
)

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest")


# ─────────────────────────────────────────────────────────────
# Normalisation helpers
# ─────────────────────────────────────────────────────────────

_ABBREVIATIONS = {
    r"\bpvt\.?\s*ltd\.?\b":     "private limited",
    r"\bp\.?\s*ltd\.?\b":       "private limited",
    r"\bltd\.?\b":              "limited",
    r"\bprivate limited\b":     "private limited",
    r"\bllp\b":                 "llp",
    r"\bengg\.?\b":             "engineering",
    r"\bmfg\.?\b":              "manufacturing",
    r"\bmfrs\.?\b":             "manufacturers",
    r"\bmfr\.?\b":              "manufacturer",
    r"\binds\.?\b":             "industries",
    r"\bindus\.?\b":            "industries",
    r"\bent\.?\b":              "enterprises",
    r"\bentp\.?\b":             "enterprises",
    r"\benterp\.?\b":           "enterprises",
    r"\bco\.?\b":               "company",
    r"\bcorp\.?\b":             "corporation",
    r"\bintl\.?\b":             "international",
    r"\bindo\.?\b":             "india",
    r"\bbglu\.?\b":             "bengaluru",
    r"\bblore\.?\b":            "bengaluru",
    r"\bb\'lore\.?\b":          "bengaluru",
    r"\bbangalore\.?\b":        "bengaluru",
}


def normalise_name(name: Optional[str]) -> tuple[Optional[str], list[str]]:
    """
    Returns (normalised_name, sorted_tokens).
    Steps:
      1. Unicode NFC normalisation
      2. Lowercase
      3. Strip punctuation (keep letters, digits, spaces)
      4. Expand abbreviations
      5. Collapse whitespace
      6. Tokenise and sort (for blocking)
    """
    if not name:
        return None, []

    s = unicodedata.normalize("NFC", name)
    s = s.lower()
    # Keep only letters, digits, spaces
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    # Expand abbreviations
    for pattern, replacement in _ABBREVIATIONS.items():
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = sorted([t for t in s.split() if len(t) > 1])
    return s, tokens


def soundex(word: str) -> str:
    """Basic Soundex implementation for phonetic blocking."""
    if not word:
        return ""
    word = word.upper()
    codes = {"BFPV": "1", "CGJKQSXYZ": "2", "DT": "3",
             "L": "4", "MN": "5", "R": "6"}
    result = word[0]
    prev = ""
    for char in word[1:]:
        code = ""
        for letters, c in codes.items():
            if char in letters:
                code = c
                break
        if code and code != prev:
            result += code
        prev = code
    result = (result + "000")[:4]
    return result


def normalise_address(addr: Optional[str]) -> Optional[str]:
    """Produce a canonical, comparable address string."""
    if not addr:
        return None
    s = unicodedata.normalize("NFC", addr).lower()
    s = re.sub(r"[^a-z0-9\s,/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def validate_pan(pan: Optional[str]) -> tuple[Optional[str], bool]:
    if not pan:
        return None, False
    pan = pan.strip().upper().replace(" ", "")
    valid = bool(re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan))
    return pan, valid


def validate_gstin(gstin: Optional[str]) -> tuple[Optional[str], bool]:
    if not gstin:
        return None, False
    gstin = gstin.strip().upper().replace(" ", "")
    valid = bool(re.match(r"^\d{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", gstin))
    return gstin, valid


def extract_normalised_fields(source: DepartmentSource, parsed_record) -> dict:
    """
    Extract normalised fields from a parsed Pydantic record.
    Returns a dict matching the normalised_records table columns.
    """
    # --- Name ---
    raw_name = None
    if source == DepartmentSource.BBMP:
        raw_name = parsed_record.business_name
    elif source == DepartmentSource.ESCOM:
        raw_name = parsed_record.consumer_name
    elif source == DepartmentSource.LABOUR:
        raw_name = parsed_record.establishment_name
    elif source == DepartmentSource.FACTORIES:
        raw_name = parsed_record.factory_name

    norm_name, tokens = normalise_name(raw_name)
    soundex_hashes = [soundex(t) for t in tokens[:2]] if tokens else []

    # --- Address ---
    raw_addr = getattr(parsed_record, "business_address", None) or \
               getattr(parsed_record, "service_address", None) or \
               getattr(parsed_record, "address", None) or \
               getattr(parsed_record, "factory_address", None)
    norm_addr = normalise_address(raw_addr)

    # --- Identifiers ---
    pan_raw = getattr(parsed_record, "pan", None)
    gstin_raw = getattr(parsed_record, "gstin", None)
    pan, pan_valid = validate_pan(pan_raw)
    gstin, gstin_valid = validate_gstin(gstin_raw)
    gstin_prefix = gstin[:12] if gstin and gstin_valid else None

    # --- Phone ---
    phone_raw = getattr(parsed_record, "mobile", None)
    phone = None
    if phone_raw:
        phone = re.sub(r"[^\d]", "", phone_raw)[-10:]

    # --- Sector ---
    sector = None
    if source == DepartmentSource.BBMP:
        sector = parsed_record.trade_category
    elif source == DepartmentSource.LABOUR:
        sector = parsed_record.industry_class
    elif source == DepartmentSource.FACTORIES:
        sector = getattr(parsed_record, "product_description", None)

    # --- Registration year ---
    reg_year = None
    date_field = getattr(parsed_record, "licence_issue_date", None) or \
                 getattr(parsed_record, "connection_date", None) or \
                 getattr(parsed_record, "coverage_date", None)
    if date_field:
        try:
            reg_year = int(str(date_field)[:4])
        except (ValueError, TypeError):
            pass

    issues = getattr(parsed_record, "validation_issues", [])

    return {
        "name_original":        raw_name,
        "name_normalised":      norm_name,
        "name_tokens":          tokens,
        "name_soundex":         soundex_hashes,
        "addr_full_normalised": norm_addr,
        "addr_pin_code":        getattr(parsed_record, "pin_code", None),
        "pan":                  pan,
        "pan_valid":            pan_valid,
        "gstin":                gstin,
        "gstin_valid":          gstin_valid,
        "gstin_prefix":         gstin_prefix,
        "phone_normalised":     phone,
        "email_normalised":     getattr(parsed_record, "email", None),
        "sector":               sector,
        "registration_year":    reg_year,
        "identifier_issues":    issues,
    }


# ─────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────

def get_connection(db_url: str):
    return psycopg2.connect(db_url)


def insert_raw_ingest_batch(conn, rows: list[dict]) -> dict[str, int]:
    """
    Bulk insert raw ingest rows. Returns {source_record_id: raw_ingest_id}.

    ON CONFLICT DO NOTHING means RETURNING only gives back newly inserted rows.
    We then do a second SELECT to pick up IDs for rows that already existed,
    so the returned map is always complete regardless of whether this is a
    first run or a re-run.
    """
    if not rows:
        return {}

    source = rows[0]["source"]
    source_record_ids = [r["source_record_id"] for r in rows]

    with conn.cursor() as cur:
        values = [
            (
                r["source"], r["source_record_id"],
                json.dumps(r["raw_payload"]),
                r["ingest_batch_id"], True  # is_scrambled
            )
            for r in rows
        ]
        execute_values(
            cur,
            """
            INSERT INTO raw_ingest
                (source, source_record_id, raw_payload, ingest_batch_id, is_scrambled)
            VALUES %s
            ON CONFLICT (source, source_record_id, ingest_batch_id) DO NOTHING
            """,
            values,
        )

        # Fetch IDs for ALL records (new + pre-existing) in one query.
        # Use DISTINCT ON to get the latest raw_ingest row per source_record_id.
        cur.execute(
            """
            SELECT DISTINCT ON (source_record_id)
                source_record_id, id
            FROM raw_ingest
            WHERE source = %s
              AND source_record_id = ANY(%s)
            ORDER BY source_record_id, id DESC
            """,
            (source, source_record_ids),
        )
        result = dict(cur.fetchall())

    conn.commit()
    return result   # {source_record_id: id}


def insert_normalised_batch(conn, rows: list[dict]) -> None:
    """Bulk insert normalised records."""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO normalised_records (
                raw_ingest_id, source, source_record_id,
                name_original, name_normalised, name_tokens, name_soundex,
                addr_full_normalised, addr_pin_code,
                pan, pan_valid, gstin, gstin_valid, gstin_prefix,
                phone_normalised, email_normalised,
                sector, registration_year, identifier_issues
            ) VALUES %s
            ON CONFLICT (source, source_record_id) DO UPDATE SET
                name_normalised       = EXCLUDED.name_normalised,
                name_tokens           = EXCLUDED.name_tokens,
                pan                   = EXCLUDED.pan,
                pan_valid             = EXCLUDED.pan_valid,
                gstin                 = EXCLUDED.gstin,
                gstin_valid           = EXCLUDED.gstin_valid,
                gstin_prefix          = EXCLUDED.gstin_prefix,
                normalisation_version = normalised_records.normalisation_version + 1,
                normalised_at         = NOW()
            """,
            [
                (
                    r["raw_ingest_id"], r["source"], r["source_record_id"],
                    r["name_original"], r["name_normalised"],
                    r["name_tokens"], r["name_soundex"],
                    r["addr_full_normalised"], r["addr_pin_code"],
                    r["pan"], r["pan_valid"], r["gstin"], r["gstin_valid"], r["gstin_prefix"],
                    r["phone_normalised"], r["email_normalised"],
                    r["sector"], r["registration_year"], r["identifier_issues"],
                )
                for r in rows
            ],
            template="""(
                %s, %s, %s,
                %s, %s, %s::text[], %s::text[],
                %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s::text[]
            )""",
        )
    conn.commit()


def insert_events_batch(conn, rows: list[dict], batch_id: str) -> int:
    """
    Bulk insert activity events. Returns the number of rows actually inserted.

    Deduplication key: (source, source_record_id, event_category, event_date).
    Re-runs are safe — duplicate events are skipped, not doubled.
    """
    if not rows:
        return 0

    # Deduplicate in Python on the natural key before hitting the DB
    seen: set[tuple] = set()
    deduped = []
    for r in rows:
        key = (r["source"], r["source_record_id"], r["event_category"], str(r["event_date"]))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # ── FIX: use explicit JOIN with per-column casts instead of row-constructor
    # IN (SELECT unnest(...)) which confuses Postgres when enum columns are
    # compared against text in a tuple context.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ae.source,
                   ae.source_record_id,
                   ae.event_category,
                   ae.event_date::text
            FROM activity_events ae
            JOIN (
                SELECT
                    unnest(%s::department_source[]) AS source,
                    unnest(%s::text[])              AS source_record_id,
                    unnest(%s::event_category[])    AS event_category,
                    unnest(%s::date[])              AS event_date
            ) chk
              ON ae.source           = chk.source
             AND ae.source_record_id = chk.source_record_id
             AND ae.event_category   = chk.event_category
             AND ae.event_date       = chk.event_date
            """,
            (
                [r["source"] for r in deduped],
                [r["source_record_id"] for r in deduped],
                [r["event_category"] for r in deduped],
                [str(r["event_date"]) for r in deduped],
            ),
        )
        already_exists: set[tuple] = {
            (row[0], row[1], row[2], str(row[3])) for row in cur.fetchall()
        }

    new_rows = [
        r for r in deduped
        if (r["source"], r["source_record_id"], r["event_category"], str(r["event_date"]))
        not in already_exists
    ]

    if not new_rows:
        conn.commit()
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO activity_events
                (source, source_record_id, event_category, event_date,
                 is_terminal, is_high_reliability, event_payload, ingest_batch_id)
            VALUES %s
            """,
            [
                (
                    r["source"],
                    r["source_record_id"],
                    r["event_category"],
                    r["event_date"],
                    r.get("is_terminal", False),
                    r.get("is_high_reliability", False),
                    json.dumps(r.get("payload") or {}),
                    batch_id,
                )
                for r in new_rows
            ],
        )
    conn.commit()
    return len(new_rows)


# ─────────────────────────────────────────────────────────────
# Source file mappings
# ─────────────────────────────────────────────────────────────

SOURCE_CONFIG = {
    DepartmentSource.BBMP: {
        "file":  "bbmp_trade_licences.csv",
        "pk":    "trade_licence_no",
        "model": BBMPRecord,
    },
    DepartmentSource.ESCOM: {
        "file":  "escom_connections.csv",
        "pk":    "consumer_no",
        "model": ESCOMRecord,
    },
    DepartmentSource.LABOUR: {
        "file":  "labour_establishments.csv",
        "pk":    "pf_code",
        "model": LabourRecord,
    },
    DepartmentSource.FACTORIES: {
        "file":  "factories_licences.csv",
        "pk":    "factory_licence_no",
        "model": FactoriesRecord,
    },
}


# ─────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────

def run_ingest(data_dir: Path, db_url: str, batch_id: str) -> dict:
    """
    Full ingestion run:
    1. Read CSVs
    2. Validate (Pydantic)
    3. Normalise
    4. Load into Postgres (raw_ingest + normalised_records)
    5. Load activity events
    """
    conn = get_connection(db_url)
    stats = {
        "batch_id":   batch_id,
        "started_at": datetime.utcnow().isoformat(),
        "sources":    {},
        "events":     {},
        "failures":   [],
    }

    # ── Process each department source ──────────────────────
    for source, cfg in SOURCE_CONFIG.items():
        csv_path = data_dir / cfg["file"]
        if not csv_path.exists():
            log.warning(f"File not found, skipping: {csv_path}")
            continue

        log.info(f"Ingesting {source.value} from {csv_path.name}")
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        df = df.where(df.ne(""), None)  # empty strings → None

        raw_rows       = []
        norm_to_insert = []
        failures       = []

        for _, row in df.iterrows():
            row_dict = {k: (None if pd.isna(v) else v)
                        for k, v in row.to_dict().items()}

            ingest_rec, failure = parse_raw_record(source, row_dict, batch_id)

            if failure:
                failures.append({
                    "source": source.value,
                    "error":  failure.error_details,
                    "record": str(row_dict.get(cfg["pk"], "??")),
                })
                # Still ingest raw — never drop a record
                raw_rows.append({
                    "source":           source.value,
                    "source_record_id": f"{row_dict.get(cfg['pk'], 'unknown')}_{uuid.uuid4().hex[:6]}",
                    "raw_payload":      row_dict,
                    "ingest_batch_id":  str(batch_id),
                })
                continue

            raw_rows.append({
                "source":           source.value,
                "source_record_id": ingest_rec.source_record_id,
                "raw_payload":      ingest_rec.raw_payload,
                "ingest_batch_id":  str(batch_id),
            })

        # Bulk insert raw
        id_map = insert_raw_ingest_batch(conn, raw_rows)
        log.info(f"  raw_ingest: {len(id_map)} rows inserted")

        # Build normalised records
        for raw_row in raw_rows:
            src_rec_id = raw_row["source_record_id"]
            raw_id = id_map.get(src_rec_id)
            if not raw_id:
                continue

            parsed_row, _ = parse_raw_record(
                source, raw_row["raw_payload"], batch_id
            )
            if not parsed_row:
                continue

            model_cls    = cfg["model"]
            parsed_model = model_cls.model_validate(raw_row["raw_payload"])
            norm_fields  = extract_normalised_fields(source, parsed_model)
            norm_to_insert.append({
                "raw_ingest_id":    raw_id,
                "source":           source.value,
                "source_record_id": src_rec_id,
                **norm_fields,
            })

        insert_normalised_batch(conn, norm_to_insert)
        log.info(f"  normalised_records: {len(norm_to_insert)} rows upserted")

        stats["sources"][source.value] = {
            "total":      len(df),
            "new_raw":    len(id_map),
            "normalised": len(norm_to_insert),
            "failures":   len(failures),
        }
        if failures:
            stats["failures"].extend(failures)
            log.warning(f"  {len(failures)} validation failures for {source.value}")

    # ── Activity events ──────────────────────────────────────
    events_path = data_dir / "activity_events.csv"
    if events_path.exists():
        log.info(f"Ingesting activity events from {events_path.name}")
        ev_df = pd.read_csv(events_path, dtype=str, keep_default_na=False)
        ev_df = ev_df.where(ev_df.ne(""), None)
        ev_rows = ev_df.to_dict("records")

        # Convert boolean strings
        for r in ev_rows:
            for bool_field in ("is_terminal", "is_high_reliability"):
                r[bool_field] = r.get(bool_field) in ("True", "true", "1")
            if "payload" not in r:
                r["payload"] = "{}"

        events_inserted = insert_events_batch(conn, ev_rows, batch_id)
        events_skipped  = len(ev_rows) - events_inserted
        stats["events"] = {
            "total":    len(ev_rows),
            "inserted": events_inserted,
            "skipped":  events_skipped,
        }
        log.info(
            f"  Activity events: {events_inserted} new rows inserted "
            f"({events_skipped} skipped as duplicates)"
        )

    conn.close()
    stats["finished_at"] = datetime.utcnow().isoformat()
    return stats


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="UBID Platform — Ingestion Pipeline")
    parser.add_argument(
        "--data-dir", default=os.environ.get("DATA_DIR", "./synthetic_data"),
        help="Path to directory containing the 4 source CSV files"
    )
    parser.add_argument(
        "--db-url", default=os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/ubid_platform"),
        help="PostgreSQL connection URL"
    )
    parser.add_argument(
        "--batch-id", default=os.environ.get("BATCH_ID", str(uuid.uuid4())),
        help="UUID for this ingest batch (auto-generated if not set)"
    )
    args = parser.parse_args()

    log.info("━" * 60)
    log.info("UBID Platform — Ingestion Pipeline")
    log.info(f"Batch ID:  {args.batch_id}")
    log.info(f"Data dir:  {args.data_dir}")
    log.info(f"DB URL:    {args.db_url.split('@')[-1]}")  # hide credentials
    log.info("━" * 60)

    stats = run_ingest(
        data_dir=Path(args.data_dir),
        db_url=args.db_url,
        batch_id=args.batch_id,
    )

    log.info("\n" + "━" * 60)
    log.info("INGEST COMPLETE")
    log.info("━" * 60)
    for source, s in stats["sources"].items():
        log.info(
            f"{source:15s}  total={s['total']:4d}  new_raw={s['new_raw']:4d}  "
            f"normalised={s['normalised']:4d}  failures={s['failures']:3d}"
        )
    if stats.get("events"):
        ev = stats["events"]
        log.info(
            f"{'Events':15s}  total={ev['total']:4d}  "
            f"new={ev['inserted']:4d}  skipped={ev['skipped']:4d}"
        )
    if stats["failures"]:
        log.warning(
            f"\n{len(stats['failures'])} total validation failures. "
            f"Check quarantine table or rerun with --verbose."
        )
    log.info("━" * 60)


if __name__ == "__main__":
    main()