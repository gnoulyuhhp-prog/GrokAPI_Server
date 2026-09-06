CREATE TABLE IF NOT EXISTS api_key_sales_reservations (
    id BIGSERIAL PRIMARY KEY,
    external_reference VARCHAR(128) NOT NULL UNIQUE,
    operation VARCHAR(20) NOT NULL CHECK (operation IN ('new_key', 'renew_key', 'batch_new_key')),
    group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
    requested_tokens BIGINT NOT NULL CHECK (requested_tokens > 0),
    quota_delta NUMERIC(20, 8) NOT NULL CHECK (quota_delta > 0),
    target_key_hash CHAR(64) NOT NULL DEFAULT '',
    state VARCHAR(16) NOT NULL DEFAULT 'held',
    fulfilled_api_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (state IN ('held', 'completed', 'released', 'expired')),
    CHECK (
        (operation = 'renew_key' AND target_key_hash <> '') OR
        (operation <> 'renew_key' AND target_key_hash = '')
    )
);

CREATE INDEX IF NOT EXISTS idx_api_key_sales_reservations_expiry
    ON api_key_sales_reservations(group_id, state, expires_at);

CREATE INDEX IF NOT EXISTS idx_api_key_sales_reservations_fulfilled_key
    ON api_key_sales_reservations(fulfilled_api_key_id)
    WHERE fulfilled_api_key_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS api_key_sales_reservation_items (
    id BIGSERIAL PRIMARY KEY,
    reservation_id BIGINT NOT NULL REFERENCES api_key_sales_reservations(id) ON DELETE CASCADE,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    requested_tokens BIGINT NOT NULL CHECK (requested_tokens > 0),
    quota_delta NUMERIC(20, 8) NOT NULL CHECK (quota_delta > 0),
    api_key_id BIGINT NOT NULL UNIQUE REFERENCES api_keys(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (reservation_id, item_index)
);

COMMENT ON TABLE api_key_sales_reservations IS
    'Atomic capacity holds for trusted Grok API key sales consumers';

COMMENT ON COLUMN api_key_sales_reservations.target_key_hash IS
    'SHA-256 hash of a renewal target; plaintext API keys are never persisted here';

COMMENT ON TABLE api_key_sales_reservation_items IS
    'Fulfilled keys belonging to an all-or-nothing batch reservation';
