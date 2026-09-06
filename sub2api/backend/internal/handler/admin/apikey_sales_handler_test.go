package admin

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func signedSalesRequest(t *testing.T, handler gin.HandlerFunc, body string) *httptest.ResponseRecorder {
	t.Helper()
	const secret = "test-sales-secret"
	t.Setenv("SUB2API_SALES_SECRET", secret)
	t.Setenv("SUB2API_SALES_ALLOWED_CIDRS", "192.0.2.1/32")
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(timestamp + "\n" + body))

	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest(http.MethodPost, "/sales", bytes.NewBufferString(body))
	ctx.Request.Header.Set("Content-Type", "application/json")
	ctx.Request.Header.Set("X-Sales-Timestamp", timestamp)
	ctx.Request.Header.Set("X-Sales-Signature", hex.EncodeToString(mac.Sum(nil)))
	handler(ctx)
	return recorder
}

func TestAPIKeySaleAvailabilityReturnsExactIntegerCapacity(t *testing.T) {
	gin.SetMode(gin.TestMode)
	handler := NewAdminAPIKeyHandler(&stubAdminService{})
	recorder := signedSalesRequest(t, handler.SaleAvailability, `{"group_id":34}`)

	require.Equal(t, http.StatusOK, recorder.Code)
	var payload struct {
		Data struct {
			AvailableTokens int64 `json:"available_tokens"`
			SuggestedTokens int64 `json:"suggested_tokens"`
		} `json:"data"`
	}
	require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &payload))
	require.Equal(t, int64(6_588_203), payload.Data.AvailableTokens)
	require.Equal(t, int64(6_588_000), payload.Data.SuggestedTokens)
}

func TestAPIKeySaleIdentityRequiresActiveGrokGroupAndEligibleUser(t *testing.T) {
	gin.SetMode(gin.TestMode)
	svc := newStubAdminService()
	svc.groups[0].Platform = service.PlatformGrok
	svc.groups[0].IsExclusive = true
	svc.users[0].AllowedGroups = []int64{svc.groups[0].ID}
	handler := NewAdminAPIKeyHandler(svc)
	recorder := signedSalesRequest(t, handler.VerifySaleIdentity, `{"group_id":2,"user_id":1}`)

	require.Equal(t, http.StatusOK, recorder.Code)
	var payload struct {
		Data struct {
			GroupID          int64  `json:"group_id"`
			UserID           int64  `json:"user_id"`
			GroupPlatform    string `json:"group_platform"`
			GroupActive      bool   `json:"group_active"`
			UserActive       bool   `json:"user_active"`
			UserCanBindGroup bool   `json:"user_can_bind_group"`
			Eligible         bool   `json:"eligible"`
		} `json:"data"`
	}
	require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &payload))
	require.Equal(t, int64(2), payload.Data.GroupID)
	require.Equal(t, int64(1), payload.Data.UserID)
	require.Equal(t, service.PlatformGrok, payload.Data.GroupPlatform)
	require.True(t, payload.Data.GroupActive)
	require.True(t, payload.Data.UserActive)
	require.True(t, payload.Data.UserCanBindGroup)
	require.True(t, payload.Data.Eligible)
}

func TestAPIKeySaleIdentityFailsClosedForWrongPlatformInactiveOrUnauthorized(t *testing.T) {
	gin.SetMode(gin.TestMode)
	tests := []struct {
		name   string
		mutate func(*stubAdminService)
		reason string
	}{
		{"wrong platform", func(s *stubAdminService) {}, "GROK_GROUP_PLATFORM_MISMATCH"},
		{"inactive group", func(s *stubAdminService) {
			s.groups[0].Platform = service.PlatformGrok
			s.groups[0].Status = service.StatusDisabled
		}, "GROK_GROUP_INACTIVE"},
		{"inactive user", func(s *stubAdminService) {
			s.groups[0].Platform = service.PlatformGrok
			s.users[0].Status = service.StatusDisabled
		}, "GROK_SALES_USER_INACTIVE"},
		{"exclusive unauthorized", func(s *stubAdminService) { s.groups[0].Platform = service.PlatformGrok; s.groups[0].IsExclusive = true }, "GROK_SALES_USER_NOT_ELIGIBLE"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			svc := newStubAdminService()
			test.mutate(svc)
			handler := NewAdminAPIKeyHandler(svc)
			recorder := signedSalesRequest(t, handler.VerifySaleIdentity, `{"group_id":2,"user_id":1}`)
			require.NotEqual(t, http.StatusOK, recorder.Code)
			require.Contains(t, recorder.Body.String(), test.reason)
		})
	}
}

func TestAPIKeySaleReserveReturnsStableCapacityConflictWithoutTargetKey(t *testing.T) {
	gin.SetMode(gin.TestMode)
	handler := NewAdminAPIKeyHandler(&stubAdminService{})
	body := `{"external_reference":"grok-order-9","operation":"renew_key","group_id":34,"requested_tokens":10000000,"target_key":"sk-super-secret-renewal-key-123456","expires_at":"2030-01-01T00:00:00Z"}`
	recorder := signedSalesRequest(t, handler.ReserveSale, body)

	require.Equal(t, http.StatusConflict, recorder.Code)
	require.NotContains(t, recorder.Body.String(), "sk-super-secret-renewal-key")
	var payload struct {
		Reason   string            `json:"reason"`
		Metadata map[string]string `json:"metadata"`
	}
	require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &payload))
	require.Equal(t, "INSUFFICIENT_GROK_CAPACITY", payload.Reason)
	require.Equal(t, "6588203", payload.Metadata["available_tokens"])
	require.Equal(t, "6588000", payload.Metadata["suggested_tokens"])
}
