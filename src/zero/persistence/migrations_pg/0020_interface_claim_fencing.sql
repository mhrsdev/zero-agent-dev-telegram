-- GENERATED from 0020_interface_claim_fencing.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Fence interface event completion/failure to the specific lease claim.
ALTER TABLE interface_event_claims ADD COLUMN claim_token TEXT;

CREATE INDEX IF NOT EXISTS idx_interface_event_claims_token
    ON interface_event_claims(platform, binding_scope, external_event_id, claim_token);