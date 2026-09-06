package repository

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestGrokSalesReservationMigrationContract(t *testing.T) {
	_, currentFile, _, ok := runtime.Caller(0)
	require.True(t, ok)

	migrationPath := filepath.Join(filepath.Dir(currentFile), "..", "..", "migrations", "223_grok_sales_capacity_reservations.sql")
	contents, err := os.ReadFile(migrationPath)
	require.NoError(t, err)

	sql := string(contents)
	for _, fragment := range []string{
		"CREATE TABLE IF NOT EXISTS api_key_sales_reservations",
		"external_reference VARCHAR(128) NOT NULL UNIQUE",
		"requested_tokens BIGINT NOT NULL CHECK (requested_tokens > 0)",
		"quota_delta NUMERIC(20, 8) NOT NULL CHECK (quota_delta > 0)",
		"state VARCHAR(16) NOT NULL",
		"CHECK (state IN ('held', 'completed', 'released', 'expired'))",
		"target_key_hash CHAR(64) NOT NULL DEFAULT ''",
		"expires_at TIMESTAMPTZ NOT NULL",
		"fulfilled_api_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL",
		"CREATE INDEX IF NOT EXISTS idx_api_key_sales_reservations_expiry",
		"CREATE TABLE IF NOT EXISTS api_key_sales_reservation_items",
		"UNIQUE (reservation_id, item_index)",
		"api_key_id BIGINT NOT NULL UNIQUE REFERENCES api_keys(id) ON DELETE RESTRICT",
	} {
		require.Contains(t, sql, fragment)
	}
}
