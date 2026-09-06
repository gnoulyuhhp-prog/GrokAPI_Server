CREATE TABLE IF NOT EXISTS api_key_sales_fulfillments (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    operation VARCHAR(20) NOT NULL CHECK (operation IN ('new_key', 'renew_key')),
    request_fingerprint CHAR(64) NOT NULL,
    api_key_id BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE RESTRICT,
    quota_delta DECIMAL(20, 8) NOT NULL CHECK (quota_delta > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_key_sales_fulfillments_api_key_id
    ON api_key_sales_fulfillments(api_key_id, created_at DESC);

COMMENT ON TABLE api_key_sales_fulfillments IS
    'Idempotency ledger for API key quota sales fulfilled by trusted internal systems';
