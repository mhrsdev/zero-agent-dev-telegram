-- Fence interface event completion/failure to the specific lease claim.
ALTER TABLE interface_event_claims ADD COLUMN claim_token TEXT;

CREATE INDEX IF NOT EXISTS idx_interface_event_claims_token
    ON interface_event_claims(platform, binding_scope, external_event_id, claim_token);
