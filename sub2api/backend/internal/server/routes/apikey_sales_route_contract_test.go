package routes

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// This source-to-router contract catches a compiled handler that was never
// registered, which previously shipped availability while identity returned 404.
func TestAdminAPIKeySalesIdentityRouteIsRegistered(t *testing.T) {
	_, current, _, ok := runtime.Caller(0)
	require.True(t, ok)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(current), "admin.go"))
	require.NoError(t, err)
	source := string(raw)
	require.Equal(t, 1, strings.Count(source, `apiKeys.POST("/sales/verify-identity", h.Admin.APIKey.VerifySaleIdentity)`))
}
