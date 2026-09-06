package admin

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

const maxAPIKeySaleBodyBytes = 512 * 1024

type apiKeySaleRequest struct {
	ReservationID int64   `json:"reservation_id"`
	Operation     string  `json:"operation"`
	UserID        int64   `json:"user_id"`
	GroupID       int64   `json:"group_id"`
	QuotaDelta    float64 `json:"quota_delta"`
	Name          string  `json:"name"`
	TargetKey     string  `json:"target_key"`
}

type apiKeySaleAvailabilityRequest struct {
	GroupID int64 `json:"group_id"`
}

type apiKeySaleIdentityRequest struct {
	GroupID int64 `json:"group_id"`
	UserID  int64 `json:"user_id"`
}

type apiKeySaleIdentityResponse struct {
	GroupID          int64  `json:"group_id"`
	UserID           int64  `json:"user_id"`
	GroupPlatform    string `json:"group_platform"`
	GroupActive      bool   `json:"group_active"`
	UserActive       bool   `json:"user_active"`
	UserCanBindGroup bool   `json:"user_can_bind_group"`
	Eligible         bool   `json:"eligible"`
}

type apiKeySaleReserveRequest struct {
	ExternalReference string    `json:"external_reference"`
	Operation         string    `json:"operation"`
	GroupID           int64     `json:"group_id"`
	RequestedTokens   int64     `json:"requested_tokens"`
	TargetKey         string    `json:"target_key"`
	ExpiresAt         time.Time `json:"expires_at"`
}

type apiKeySaleReleaseRequest struct {
	ReservationID int64 `json:"reservation_id"`
}

type apiKeySaleBatchRequest struct {
	ReservationID int64                         `json:"reservation_id"`
	GroupID       int64                         `json:"group_id"`
	Items         []service.APIKeySaleBatchItem `json:"items"`
}

type apiKeySaleLookupRequest struct {
	TargetKey string `json:"target_key"`
}

type apiKeySaleResponse struct {
	Operation   string  `json:"operation"`
	Idempotent  bool    `json:"idempotent"`
	APIKeyID    int64   `json:"api_key_id"`
	Key         string  `json:"key,omitempty"`
	MaskedKey   string  `json:"masked_key"`
	Status      string  `json:"status"`
	QuotaLimit  float64 `json:"quota_limit"`
	QuotaUsed   float64 `json:"quota_used"`
	QuotaRemain float64 `json:"quota_remaining"`
}

func maskSalesAPIKey(value string) string {
	value = strings.TrimSpace(value)
	if len(value) <= 12 {
		return "***"
	}
	return value[:7] + "..." + value[len(value)-5:]
}

func salesClientIPAllowed(rawIP, configured string) bool {
	ip := net.ParseIP(strings.TrimSpace(rawIP))
	if ip == nil || strings.TrimSpace(configured) == "" {
		return false
	}
	for _, rawRule := range strings.Split(configured, ",") {
		rule := strings.TrimSpace(rawRule)
		if rule == "" {
			continue
		}
		if allowedIP := net.ParseIP(rule); allowedIP != nil && allowedIP.Equal(ip) {
			return true
		}
		if _, network, err := net.ParseCIDR(rule); err == nil && network.Contains(ip) {
			return true
		}
	}
	return false
}

func verifyAPIKeySaleRequest(c *gin.Context, body []byte) bool {
	secret := strings.TrimSpace(os.Getenv("SUB2API_SALES_SECRET"))
	allowedCIDRs := strings.TrimSpace(os.Getenv("SUB2API_SALES_ALLOWED_CIDRS"))
	if secret == "" || allowedCIDRs == "" {
		response.ErrorWithDetails(c, http.StatusServiceUnavailable, "Internal API key sales are not configured", "API_KEY_SALES_NOT_CONFIGURED", nil)
		return false
	}
	if !salesClientIPAllowed(c.ClientIP(), allowedCIDRs) {
		response.ErrorWithDetails(c, http.StatusForbidden, "Source IP is not allowed", "API_KEY_SALES_IP_DENIED", nil)
		return false
	}
	timestampText := strings.TrimSpace(c.GetHeader("X-Sales-Timestamp"))
	timestamp, err := strconv.ParseInt(timestampText, 10, 64)
	if err != nil || time.Since(time.Unix(timestamp, 0)).Abs() > 5*time.Minute {
		response.ErrorWithDetails(c, http.StatusUnauthorized, "Request timestamp is invalid", "INVALID_SALES_TIMESTAMP", nil)
		return false
	}
	provided, err := hex.DecodeString(strings.TrimSpace(c.GetHeader("X-Sales-Signature")))
	if err != nil {
		response.ErrorWithDetails(c, http.StatusUnauthorized, "Request signature is invalid", "INVALID_SALES_SIGNATURE", nil)
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(timestampText + "\n"))
	_, _ = mac.Write(body)
	expected := mac.Sum(nil)
	if len(provided) != len(expected) || subtle.ConstantTimeCompare(provided, expected) != 1 {
		response.ErrorWithDetails(c, http.StatusUnauthorized, "Request signature is invalid", "INVALID_SALES_SIGNATURE", nil)
		return false
	}
	return true
}

func readVerifiedAPIKeySaleBody(c *gin.Context) ([]byte, bool) {
	body, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, maxAPIKeySaleBodyBytes))
	if err != nil {
		response.BadRequest(c, "Request body is invalid")
		return nil, false
	}
	if !verifyAPIKeySaleRequest(c, body) {
		return nil, false
	}
	return body, true
}

// VerifySaleIdentity proves that the exact group/user pair may own newly sold
// Grok keys. It intentionally returns no user or group display data.
// POST /api/v1/admin/api-keys/sales/verify-identity
func (h *AdminAPIKeyHandler) VerifySaleIdentity(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleIdentityRequest
	if err := json.Unmarshal(body, &req); err != nil || req.GroupID <= 0 || req.UserID <= 0 {
		response.ErrorWithDetails(c, http.StatusBadRequest, "Sales identity is invalid", "INVALID_SALES_IDENTITY", nil)
		return
	}
	group, err := h.adminService.GetGroup(c.Request.Context(), req.GroupID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if group == nil || group.ID != req.GroupID {
		response.ErrorWithDetails(c, http.StatusNotFound, "Grok sales group was not found", "GROK_GROUP_NOT_FOUND", nil)
		return
	}
	if group.Platform != service.PlatformGrok {
		response.ErrorWithDetails(c, http.StatusConflict, "Sales group is not a Grok group", "GROK_GROUP_PLATFORM_MISMATCH", nil)
		return
	}
	if !group.IsActive() {
		response.ErrorWithDetails(c, http.StatusConflict, "Grok sales group is inactive", "GROK_GROUP_INACTIVE", nil)
		return
	}
	user, err := h.adminService.GetUser(c.Request.Context(), req.UserID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if user == nil || user.ID != req.UserID || user.DeletedAt != nil {
		response.ErrorWithDetails(c, http.StatusNotFound, "Grok sales user was not found", "GROK_SALES_USER_NOT_FOUND", nil)
		return
	}
	if !user.IsActive() {
		response.ErrorWithDetails(c, http.StatusConflict, "Grok sales user is inactive", "GROK_SALES_USER_INACTIVE", nil)
		return
	}
	if !user.CanBindGroup(group.ID, group.IsExclusive) {
		response.ErrorWithDetails(c, http.StatusConflict, "Grok sales user cannot bind the group", "GROK_SALES_USER_NOT_ELIGIBLE", nil)
		return
	}
	response.Success(c, apiKeySaleIdentityResponse{
		GroupID: req.GroupID, UserID: req.UserID, GroupPlatform: group.Platform,
		GroupActive: true, UserActive: true, UserCanBindGroup: true, Eligible: true,
	})
}

// SaleAvailability returns authoritative capacity after outstanding key quota and active holds.
// POST /api/v1/admin/api-keys/sales/availability
func (h *AdminAPIKeyHandler) SaleAvailability(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleAvailabilityRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	result, err := h.adminService.AdminGetAPIKeySaleAvailability(c.Request.Context(), req.GroupID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

// ReserveSale atomically holds the full requested capacity before payment is exposed.
// POST /api/v1/admin/api-keys/sales/reserve
func (h *AdminAPIKeyHandler) ReserveSale(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleReserveRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	result, err := h.adminService.AdminReserveAPIKeySale(c.Request.Context(), service.APIKeySaleReserveInput{
		ExternalReference: req.ExternalReference,
		Operation:         req.Operation,
		GroupID:           req.GroupID,
		RequestedTokens:   req.RequestedTokens,
		TargetKey:         req.TargetKey,
		ExpiresAt:         req.ExpiresAt,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

// ReleaseSale idempotently gives unused capacity back to the pool.
// POST /api/v1/admin/api-keys/sales/release
func (h *AdminAPIKeyHandler) ReleaseSale(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleReleaseRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	result, err := h.adminService.AdminReleaseAPIKeySale(c.Request.Context(), req.ReservationID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

// FulfillSaleBatch creates every key in a reservation atomically or creates none.
// POST /api/v1/admin/api-keys/sales/fulfill-batch
func (h *AdminAPIKeyHandler) FulfillSaleBatch(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleBatchRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	result, err := h.adminService.AdminFulfillAPIKeySaleBatch(c.Request.Context(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: c.GetHeader("Idempotency-Key"),
		ReservationID:  req.ReservationID,
		GroupID:        req.GroupID,
		Items:          req.Items,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

// FulfillSale atomically creates an API key or adds quota to an existing key.
// POST /api/v1/admin/api-keys/sales/fulfill
func (h *AdminAPIKeyHandler) FulfillSale(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	result, err := h.adminService.AdminFulfillAPIKeySale(c.Request.Context(), service.APIKeySaleFulfillmentInput{
		IdempotencyKey: c.GetHeader("Idempotency-Key"),
		ReservationID:  req.ReservationID,
		Legacy:         req.ReservationID <= 0,
		Operation:      req.Operation,
		UserID:         req.UserID,
		GroupID:        req.GroupID,
		QuotaDelta:     req.QuotaDelta,
		Name:           req.Name,
		TargetKey:      req.TargetKey,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	key := result.APIKey
	remaining := key.Quota - key.QuotaUsed
	if remaining < 0 {
		remaining = 0
	}
	payload := apiKeySaleResponse{
		Operation:   result.Operation,
		Idempotent:  result.Idempotent,
		APIKeyID:    key.ID,
		MaskedKey:   maskSalesAPIKey(key.Key),
		Status:      key.Status,
		QuotaLimit:  key.Quota,
		QuotaUsed:   key.QuotaUsed,
		QuotaRemain: remaining,
	}
	if result.Operation == service.APIKeySaleNew {
		payload.Key = key.Key
	}
	response.Success(c, payload)
}

// LookupSale validates a pasted key before the customer is asked to pay.
// POST /api/v1/admin/api-keys/sales/lookup
func (h *AdminAPIKeyHandler) LookupSale(c *gin.Context) {
	body, ok := readVerifiedAPIKeySaleBody(c)
	if !ok {
		return
	}
	var req apiKeySaleLookupRequest
	if err := json.Unmarshal(body, &req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	key, err := h.adminService.AdminLookupAPIKeySale(c.Request.Context(), req.TargetKey)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	remaining := key.Quota - key.QuotaUsed
	if remaining < 0 {
		remaining = 0
	}
	response.Success(c, apiKeySaleResponse{
		APIKeyID: key.ID, MaskedKey: maskSalesAPIKey(key.Key), Status: key.Status,
		QuotaLimit: key.Quota, QuotaUsed: key.QuotaUsed, QuotaRemain: remaining,
	})
}
