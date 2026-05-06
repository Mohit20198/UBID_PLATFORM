-- migrate_day2.sql
-- Run this ONCE against ubid_platform after schema.sql from Day 1.
-- Adds the columns that the POST /decision endpoint writes.
-- All ALTER TABLE … ADD COLUMN IF NOT EXISTS are idempotent.

-- ── audit_log: add columns needed by the decision router ─────────────────────

ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS pair_id         TEXT,
    ADD COLUMN IF NOT EXISTS left_source     TEXT,
    ADD COLUMN IF NOT EXISTS left_record_id  TEXT,
    ADD COLUMN IF NOT EXISTS right_source    TEXT,
    ADD COLUMN IF NOT EXISTS right_record_id TEXT,
    ADD COLUMN IF NOT EXISTS action          TEXT,          -- 'MERGE' | 'REJECT' | 'UNMERGE'
    ADD COLUMN IF NOT EXISTS ubid_assigned   TEXT,
    ADD COLUMN IF NOT EXISTS analyst_id      TEXT DEFAULT 'anonymous',
    ADD COLUMN IF NOT EXISTS notes           TEXT,
    ADD COLUMN IF NOT EXISTS decided_at      TIMESTAMPTZ DEFAULT NOW();

-- Index for fast pair_id lookup (used by review-queue to check already-decided)
CREATE INDEX IF NOT EXISTS idx_audit_log_pair_id ON audit_log (pair_id);

-- ── ubid_registry: ensure status column exists ───────────────────────────────

ALTER TABLE ubid_registry
    ADD COLUMN IF NOT EXISTS status     TEXT DEFAULT 'ACTIVE',
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

-- ── ubid_members: ensure unique constraint for idempotent inserts ─────────────
-- The ON CONFLICT in _add_member requires a unique constraint on (ubid_id, source, source_record_id)

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_ubid_members_triple'
    ) THEN
        ALTER TABLE ubid_members
            ADD CONSTRAINT uq_ubid_members_triple
            UNIQUE (ubid, source, source_record_id);
    END IF;
END $$;

-- added_at column for ubid_members
ALTER TABLE ubid_members
    ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ DEFAULT NOW();

-- Verify
SELECT
    (SELECT COUNT(*) FROM audit_log)     AS audit_log_rows,
    (SELECT COUNT(*) FROM ubid_registry) AS ubid_registry_rows,
    (SELECT COUNT(*) FROM ubid_members)  AS ubid_members_rows;
