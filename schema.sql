-- ============================================================
-- UBID Platform - PostgreSQL 16 Schema
-- Karnataka Business Entity Resolution Platform
-- ============================================================
-- Run as: psql -U postgres -d ubid_platform -f schema.sql

BEGIN;

-- ============================================================
-- EXTENSIONS
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";   -- trigram similarity for name matching
CREATE EXTENSION IF NOT EXISTS "unaccent";  -- accent-insensitive search

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE department_source AS ENUM (
    'BBMP',           -- Bruhat Bengaluru Mahanagara Palike (Trade Licences)
    'ESCOM',          -- Electricity supply company (consumption)
    'LABOUR',         -- Labour Department (PF/ESI registrations)
    'POLLUTION',      -- Karnataka State Pollution Control Board
    'GST',            -- GST portal data
    'BWSSB',          -- Water supply
    'FACTORIES',      -- Factory inspectorate
    'COMMERCIAL_TAX'  -- Commercial Tax Department
);

CREATE TYPE identifier_type AS ENUM (
    'PAN', 'GSTIN', 'CIN', 'UDYAM', 'TRADE_LICENCE',
    'FACTORY_LICENCE', 'PF_CODE', 'ESI_CODE', 'EB_CONSUMER_NO',
    'WATER_CONN_NO', 'POLLUTION_CONSENT_NO'
);

CREATE TYPE business_status AS ENUM (
    'ACTIVE',    -- positive signals in last 12 months from 2+ sources OR 1 high-reliability source
    'DORMANT',   -- last signal 12-36 months ago, no terminal signal
    'CLOSED',    -- terminal signal present OR no signal for 36+ months
    'UNKNOWN'    -- insufficient data to classify
);

CREATE TYPE linkage_status AS ENUM (
    'AUTO_LINKED',     -- score >= 85 AND hard identifier confirmed
    'REVIEW_PENDING',  -- score 55-84, or high score without anchor, or large cluster merge
    'CONFIRMED',       -- human reviewer confirmed
    'REJECTED',        -- human reviewer rejected
    'DEFERRED',        -- needs more info / escalated
    'UNMERGED'         -- was merged, later corrected
);

CREATE TYPE decision_actor AS ENUM ('SYSTEM', 'REVIEWER', 'ADMIN');

CREATE TYPE event_category AS ENUM (
    'ELECTRICITY_READING',
    'WATER_READING',
    'LICENCE_RENEWAL',
    'PF_FILING',
    'INSPECTION',
    'SHOW_CAUSE_NOTICE',
    'COMPLIANCE_FILING',
    'LICENCE_SURRENDER',
    'CLOSURE_CERTIFICATE',
    'COURT_ORDER',
    'INSOLVENCY_FILING',
    'TRADE_LICENCE_PAYMENT',
    'TAX_FILING',
    'OTHER'
);

-- ============================================================
-- RAW INGEST TABLES (one per source, read-only mirror)
-- These are append-only. Never updated after initial load.
-- ============================================================

CREATE TABLE raw_ingest (
    id                  BIGSERIAL PRIMARY KEY,
    source              department_source NOT NULL,
    source_record_id    TEXT NOT NULL,           -- native PK from the department system
    raw_payload         JSONB NOT NULL,          -- full original record, unmodified
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingest_batch_id     UUID NOT NULL,           -- groups records from a single Airflow run
    is_scrambled        BOOLEAN NOT NULL DEFAULT TRUE,  -- always true in Round 2
    UNIQUE (source, source_record_id, ingest_batch_id)
);

CREATE INDEX idx_raw_ingest_source ON raw_ingest (source);
CREATE INDEX idx_raw_ingest_batch ON raw_ingest (ingest_batch_id);
CREATE INDEX idx_raw_ingest_payload ON raw_ingest USING gin (raw_payload);

-- ============================================================
-- NORMALISED RECORDS (canonical form, derived from raw_ingest)
-- ============================================================

CREATE TABLE normalised_records (
    id                      BIGSERIAL PRIMARY KEY,
    raw_ingest_id           BIGINT NOT NULL REFERENCES raw_ingest(id),
    source                  department_source NOT NULL,
    source_record_id        TEXT NOT NULL,

    -- Name fields
    name_original           TEXT,
    name_normalised         TEXT,           -- lowercased, abbreviations expanded
    name_tokens             TEXT[],         -- sorted token array for blocking
    name_soundex            TEXT[],         -- soundex hashes of first 2 tokens

    -- Address fields (parsed)
    addr_building           TEXT,
    addr_street             TEXT,
    addr_locality           TEXT,
    addr_city               TEXT,
    addr_pin_code           CHAR(6),
    addr_full_normalised    TEXT,           -- concatenated canonical form

    -- Identifiers
    pan                     CHAR(10),
    pan_valid               BOOLEAN,
    gstin                   CHAR(15),
    gstin_valid             BOOLEAN,
    gstin_prefix            CHAR(12),       -- first 12 chars for blocking
    cin                     TEXT,
    udyam_no                TEXT,

    -- Contact
    phone_normalised        TEXT,
    email_normalised        TEXT,

    -- Business metadata
    sector                  TEXT,
    business_type           TEXT,
    registration_year       SMALLINT,

    -- Data quality flags
    has_hard_identifier     BOOLEAN GENERATED ALWAYS AS (
                                pan_valid IS TRUE OR gstin_valid IS TRUE
                            ) STORED,
    identifier_issues       TEXT[],         -- e.g. ['PAN_FORMAT_INVALID', 'GSTIN_CHECKSUM_FAIL']
    normalisation_version   SMALLINT NOT NULL DEFAULT 1,
    normalised_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (source, source_record_id)
);

CREATE INDEX idx_norm_source ON normalised_records (source);
CREATE INDEX idx_norm_sector ON normalised_records (sector);
CREATE INDEX idx_norm_pin ON normalised_records (addr_pin_code);
CREATE INDEX idx_norm_pan ON normalised_records (pan) WHERE pan IS NOT NULL;
CREATE INDEX idx_norm_gstin ON normalised_records (gstin) WHERE gstin IS NOT NULL;
CREATE INDEX idx_norm_gstin_prefix ON normalised_records (gstin_prefix) WHERE gstin_prefix IS NOT NULL;
CREATE INDEX idx_norm_name_tokens ON normalised_records USING gin (name_tokens);
CREATE INDEX idx_norm_name_trgm ON normalised_records USING gin (name_normalised gin_trgm_ops);
CREATE INDEX idx_norm_soundex ON normalised_records USING gin (name_soundex);

-- ============================================================
-- UBID REGISTRY
-- Central entity table. One row per resolved business entity.
-- ============================================================

CREATE TABLE ubid_registry (
    ubid                TEXT PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Canonical identity (best-quality values across all linked records)
    canonical_name      TEXT,
    canonical_pin_code  CHAR(6),
    canonical_pan       CHAR(10),
    canonical_gstin     CHAR(15),
    canonical_sector    TEXT,

    -- Activity classification
    status              business_status NOT NULL DEFAULT 'UNKNOWN',
    status_confidence   NUMERIC(5,2),       -- 0-100
    status_updated_at   TIMESTAMPTZ,
    status_evidence     JSONB,              -- human-readable evidence breakdown

    -- Linkage quality
    is_anchored         BOOLEAN NOT NULL DEFAULT FALSE,  -- has confirmed hard identifier
    member_count        INT NOT NULL DEFAULT 1,          -- number of linked source records
    source_diversity    INT NOT NULL DEFAULT 1,          -- distinct sources contributing

    -- Flags
    is_flagged          BOOLEAN NOT NULL DEFAULT FALSE,   -- needs human attention
    flag_reason         TEXT
);

CREATE INDEX idx_ubid_pin ON ubid_registry (canonical_pin_code);
CREATE INDEX idx_ubid_pan ON ubid_registry (canonical_pan) WHERE canonical_pan IS NOT NULL;
CREATE INDEX idx_ubid_gstin ON ubid_registry (canonical_gstin) WHERE canonical_gstin IS NOT NULL;
CREATE INDEX idx_ubid_status ON ubid_registry (status);
CREATE INDEX idx_ubid_sector ON ubid_registry (canonical_sector);
CREATE INDEX idx_ubid_anchored ON ubid_registry (is_anchored);

-- ============================================================
-- UBID MEMBERS (the many-to-many: source records <-> UBID)
-- A source record belongs to exactly one UBID at a time.
-- History is preserved when UBIDs are split/merged.
-- ============================================================

CREATE TABLE ubid_members (
    id                  BIGSERIAL PRIMARY KEY,
    ubid                TEXT NOT NULL REFERENCES ubid_registry(ubid),
    normalised_id       BIGINT NOT NULL REFERENCES normalised_records(id),
    source              department_source NOT NULL,
    source_record_id    TEXT NOT NULL,
    joined_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at          TIMESTAMPTZ,                    -- NULL = currently active member
    removal_reason      TEXT,
    is_active           BOOLEAN GENERATED ALWAYS AS (removed_at IS NULL) STORED
);

CREATE UNIQUE INDEX idx_ubid_members_active_unique
    ON ubid_members (normalised_id) WHERE removed_at IS NULL;

CREATE INDEX idx_ubid_members_ubid ON ubid_members (ubid);
CREATE INDEX idx_ubid_members_source_rec ON ubid_members (source, source_record_id);
CREATE INDEX idx_ubid_members_active ON ubid_members (ubid) WHERE removed_at IS NULL;

-- ============================================================
-- CANDIDATE PAIRS (output of the blocking + scoring stage)
-- ============================================================

CREATE TABLE candidate_pairs (
    id                  BIGSERIAL PRIMARY KEY,
    norm_id_a           BIGINT NOT NULL REFERENCES normalised_records(id),
    norm_id_b           BIGINT NOT NULL REFERENCES normalised_records(id),

    -- Enforce canonical ordering: norm_id_a < norm_id_b (no duplicate pairs)
    CONSTRAINT pair_order CHECK (norm_id_a < norm_id_b),

    -- Feature vector (all values 0.0-1.0 unless noted)
    feat_name_tfidf         NUMERIC(5,4),   -- TF-IDF cosine similarity
    feat_name_jw            NUMERIC(5,4),   -- Jaro-Winkler edit distance
    feat_pan_exact          BOOLEAN,
    feat_gstin_exact        BOOLEAN,
    feat_gstin_prefix       BOOLEAN,        -- first 12 chars match
    feat_addr_token_overlap NUMERIC(5,4),   -- Jaccard similarity of address tokens
    feat_pin_match          BOOLEAN,
    feat_sector_match       BOOLEAN,
    feat_reg_year_diff      SMALLINT,       -- absolute difference in years
    feat_phone_match        BOOLEAN,
    feat_email_match        BOOLEAN,

    -- Scores
    log_likelihood_ratio    NUMERIC(8,4),
    confidence_score        NUMERIC(5,2),   -- 0-100, Platt-scaled

    -- Routing
    linkage_status          linkage_status NOT NULL DEFAULT 'REVIEW_PENDING',
    blocking_keys_matched   TEXT[],         -- which blocking keys triggered this pair

    -- Splink run metadata
    splink_run_id           UUID,
    scored_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (norm_id_a, norm_id_b)
);

CREATE INDEX idx_pairs_status ON candidate_pairs (linkage_status);
CREATE INDEX idx_pairs_score ON candidate_pairs (confidence_score DESC);
CREATE INDEX idx_pairs_splink_run ON candidate_pairs (splink_run_id);
CREATE INDEX idx_pairs_pending ON candidate_pairs (confidence_score DESC)
    WHERE linkage_status = 'REVIEW_PENDING';

-- ============================================================
-- AUDIT LOG (append-only, immutable)
-- Records every system and human decision.
-- ============================================================

CREATE TABLE audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            UUID NOT NULL DEFAULT uuid_generate_v4(),
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Actor
    actor_type          decision_actor NOT NULL,
    actor_id            TEXT,               -- reviewer user ID or 'SYSTEM'

    -- Subject (what was acted upon)
    entity_type         TEXT NOT NULL,      -- 'CANDIDATE_PAIR', 'UBID', 'UBID_MEMBER'
    entity_id           TEXT NOT NULL,      -- the PK of the affected entity

    -- Action
    action              TEXT NOT NULL,      -- 'AUTO_LINK', 'CONFIRM', 'REJECT', 'DEFER',
                                            -- 'UNMERGE', 'STATUS_UPDATE', 'THRESHOLD_CHANGE'
    previous_state      JSONB,
    new_state           JSONB,

    -- Decision context (snapshot at time of decision)
    confidence_score    NUMERIC(5,2),
    feature_vector      JSONB,              -- full feature breakdown
    reviewer_notes      TEXT,               -- mandatory for REJECT decisions

    -- Integrity
    previous_event_id   UUID                -- chain reference for tamper detection
);

-- Audit log is append-only: no UPDATE or DELETE allowed
-- Enforced at the application layer AND via a Postgres rule:
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE INDEX idx_audit_occurred ON audit_log (occurred_at DESC);
CREATE INDEX idx_audit_entity ON audit_log (entity_type, entity_id);
CREATE INDEX idx_audit_actor ON audit_log (actor_type, actor_id);
CREATE INDEX idx_audit_action ON audit_log (action);

-- ============================================================
-- ACTIVITY EVENTS (the 12-month transaction stream)
-- ============================================================

CREATE TABLE activity_events (
    id                  BIGSERIAL PRIMARY KEY,
    source              department_source NOT NULL,
    source_record_id    TEXT NOT NULL,      -- maps back to normalised_records
    ubid                TEXT REFERENCES ubid_registry(ubid),  -- NULL = unattributed
    is_attributed       BOOLEAN GENERATED ALWAYS AS (ubid IS NOT NULL) STORED,

    event_category      event_category NOT NULL,
    event_date          DATE NOT NULL,
    event_payload       JSONB,              -- category-specific data

    -- Pre-computed for scoring
    is_terminal         BOOLEAN NOT NULL DEFAULT FALSE,  -- closure/surrender events
    is_high_reliability BOOLEAN NOT NULL DEFAULT FALSE,  -- electricity/water readings
    signal_weight       NUMERIC(5,4),       -- assigned during inference

    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingest_batch_id     UUID NOT NULL
);

CREATE INDEX idx_events_ubid_cat ON activity_events (ubid, event_category) WHERE ubid IS NOT NULL;
CREATE INDEX idx_events_ubid ON activity_events (ubid) WHERE ubid IS NOT NULL;
CREATE INDEX idx_events_date ON activity_events (event_date DESC);
CREATE INDEX idx_events_source_rec ON activity_events (source, source_record_id);
CREATE INDEX idx_events_unattributed ON activity_events (source_record_id)
    WHERE ubid IS NULL;
CREATE INDEX idx_events_terminal ON activity_events (ubid, event_date DESC)
    WHERE is_terminal = TRUE;
CREATE INDEX idx_events_category ON activity_events (event_category, event_date DESC);

-- ============================================================
-- UNATTRIBUTED EVENTS QUEUE (events that couldn't be joined to a UBID)
-- ============================================================

CREATE TABLE unattributed_events_queue (
    id                  BIGSERIAL PRIMARY KEY,
    activity_event_id   BIGINT NOT NULL REFERENCES activity_events(id),
    source              department_source NOT NULL,
    source_record_id    TEXT NOT NULL,
    event_category      event_category NOT NULL,
    event_date          DATE NOT NULL,
    reason              TEXT NOT NULL,      -- why attribution failed
    queued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    resolution          TEXT                -- 'ATTRIBUTED', 'DISCARDED', 'DEFERRED'
);

CREATE INDEX idx_unattr_queue_resolved ON unattributed_events_queue (resolved_at)
    WHERE resolved_at IS NULL;

-- ============================================================
-- ACTIVITY INFERENCE RUNS (tracks each scoring pass)
-- ============================================================

CREATE TABLE inference_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lookback_months     INT NOT NULL DEFAULT 12,
    dormant_threshold_months    INT NOT NULL DEFAULT 12,
    closed_threshold_months     INT NOT NULL DEFAULT 36,
    ubids_processed     INT,
    ubids_active        INT,
    ubids_dormant       INT,
    ubids_closed        INT,
    ubids_unknown       INT,
    run_notes           TEXT
);

-- ============================================================
-- REVIEWER USERS (lightweight, not a full auth system)
-- ============================================================

CREATE TABLE reviewers (
    id          TEXT PRIMARY KEY,           -- e.g. 'anshika', 'arpan', 'kavyansh'
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'REVIEWER',  -- REVIEWER | ADMIN
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO reviewers (id, display_name, role) VALUES
    ('system', 'Automated System', 'ADMIN'),
    ('anshika', 'Anshika', 'ADMIN'),
    ('arpan', 'Arpan', 'REVIEWER'),
    ('kavyansh', 'Kavyansh', 'REVIEWER');

-- ============================================================
-- SPLINK MODEL VERSIONS (tracks calibration history)
-- ============================================================

CREATE TABLE splink_model_versions (
    id                  SERIAL PRIMARY KEY,
    version_id          UUID NOT NULL DEFAULT uuid_generate_v4(),
    trained_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    training_set_size   INT,
    precision_on_holdout NUMERIC(5,4),
    recall_on_holdout   NUMERIC(5,4),
    threshold_auto_link NUMERIC(5,2) NOT NULL DEFAULT 85,
    threshold_review    NUMERIC(5,2) NOT NULL DEFAULT 55,
    model_parameters    JSONB,              -- m/u probabilities per feature
    platt_scale_params  JSONB,             -- a, b coefficients
    is_current          BOOLEAN NOT NULL DEFAULT FALSE,
    promoted_at         TIMESTAMPTZ,
    notes               TEXT
);

-- Only one model is current at a time
CREATE UNIQUE INDEX idx_splink_current ON splink_model_versions (is_current)
    WHERE is_current = TRUE;

-- ============================================================
-- HELPER VIEW: review queue with full context
-- ============================================================

CREATE VIEW vw_review_queue AS
SELECT
    cp.id                   AS pair_id,
    cp.confidence_score,
    cp.linkage_status,
    cp.blocking_keys_matched,
    cp.scored_at,

    -- Record A
    nr_a.source             AS source_a,
    nr_a.source_record_id   AS source_record_id_a,
    nr_a.name_original      AS name_a,
    nr_a.name_normalised    AS name_normalised_a,
    nr_a.addr_pin_code      AS pin_a,
    nr_a.addr_full_normalised AS addr_a,
    nr_a.pan                AS pan_a,
    nr_a.gstin              AS gstin_a,
    nr_a.sector             AS sector_a,
    nr_a.registration_year  AS reg_year_a,
    nr_a.has_hard_identifier AS has_id_a,

    -- Record B
    nr_b.source             AS source_b,
    nr_b.source_record_id   AS source_record_id_b,
    nr_b.name_original      AS name_b,
    nr_b.name_normalised    AS name_normalised_b,
    nr_b.addr_pin_code      AS pin_b,
    nr_b.addr_full_normalised AS addr_b,
    nr_b.pan                AS pan_b,
    nr_b.gstin              AS gstin_b,
    nr_b.sector             AS sector_b,
    nr_b.registration_year  AS reg_year_b,
    nr_b.has_hard_identifier AS has_id_b,

    -- Feature breakdown (for UI explainability panel)
    cp.feat_name_tfidf,
    cp.feat_name_jw,
    cp.feat_pan_exact,
    cp.feat_gstin_exact,
    cp.feat_gstin_prefix,
    cp.feat_addr_token_overlap,
    cp.feat_pin_match,
    cp.feat_sector_match,
    cp.feat_reg_year_diff,
    cp.feat_phone_match,
    cp.feat_email_match,
    cp.log_likelihood_ratio

FROM candidate_pairs cp
JOIN normalised_records nr_a ON cp.norm_id_a = nr_a.id
JOIN normalised_records nr_b ON cp.norm_id_b = nr_b.id
WHERE cp.linkage_status = 'REVIEW_PENDING'
ORDER BY cp.confidence_score DESC;

-- ============================================================
-- HELPER VIEW: business activity summary per UBID
-- ============================================================

CREATE VIEW vw_ubid_activity_summary AS
SELECT
    u.ubid,
    u.canonical_name,
    u.canonical_pin_code,
    u.canonical_sector,
    u.status,
    u.status_confidence,
    u.member_count,
    u.source_diversity,
    u.is_anchored,

    -- Last signal per category
    MAX(ae.event_date) FILTER (WHERE ae.event_category = 'ELECTRICITY_READING')
        AS last_electricity_date,
    MAX(ae.event_date) FILTER (WHERE ae.event_category = 'WATER_READING')
        AS last_water_date,
    MAX(ae.event_date) FILTER (WHERE ae.event_category = 'LICENCE_RENEWAL')
        AS last_renewal_date,
    MAX(ae.event_date) FILTER (WHERE ae.event_category = 'INSPECTION')
        AS last_inspection_date,
    MAX(ae.event_date) FILTER (WHERE ae.is_terminal = TRUE)
        AS terminal_event_date,

    -- Distinct source count
    COUNT(DISTINCT ae.source) AS active_source_count,

    -- Most recent signal overall
    MAX(ae.event_date) AS last_any_signal_date,
    NOW()::DATE - MAX(ae.event_date) AS days_since_last_signal

FROM ubid_registry u
LEFT JOIN activity_events ae ON u.ubid = ae.ubid
GROUP BY u.ubid, u.canonical_name, u.canonical_pin_code,
         u.canonical_sector, u.status, u.status_confidence,
         u.member_count, u.source_diversity, u.is_anchored;

COMMIT;
