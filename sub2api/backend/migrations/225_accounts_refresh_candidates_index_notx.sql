-- Speed up background OAuth refresh candidate scans without changing account data.
-- The predicate matches the refresh worker's active-account query and keeps the
-- index small enough for the live VPS account pool.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_accounts_refresh_candidates
    ON accounts (platform, id)
    WHERE deleted_at IS NULL
      AND schedulable = TRUE
      AND status = 'active'
      AND type IN ('oauth', 'setup-token')
      AND credentials ? 'refresh_token'
      AND btrim(credentials->>'refresh_token') <> '';
