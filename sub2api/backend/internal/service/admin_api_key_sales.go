package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

const (
	APIKeySaleNew   = "new_key"
	APIKeySaleRenew = "renew_key"
	APIKeySaleBatch = "batch_new_key"

	APIKeySaleReservationHeld      = "held"
	APIKeySaleReservationCompleted = "completed"
	APIKeySaleReservationReleased  = "released"
	APIKeySaleReservationExpired   = "expired"

	grokTokensPerActiveAccountEnv       = "SUB2API_GROK_TOKENS_PER_ACTIVE_ACCOUNT"
	grokTokensPerQuotaUSDEnv            = "SUB2API_GROK_TOKENS_PER_QUOTA_USD"
	grokSaleMinimumTokens         int64 = 1_000
)

type APIKeySaleAvailability struct {
	GroupID           int64 `json:"group_id"`
	ActiveAccounts    int64 `json:"active_accounts"`
	CapacityTokens    int64 `json:"capacity_tokens"`
	OutstandingTokens int64 `json:"outstanding_tokens"`
	ReservedTokens    int64 `json:"reserved_tokens"`
	AvailableTokens   int64 `json:"available_tokens"`
	SuggestedTokens   int64 `json:"suggested_tokens"`
	MinimumTokens     int64 `json:"minimum_tokens"`
	TokensPerAccount  int64 `json:"tokens_per_active_account"`
	TokensPerQuotaUSD int64 `json:"tokens_per_quota_usd"`
}

type APIKeySaleReservation struct {
	ID                int64     `json:"id"`
	ExternalReference string    `json:"external_reference"`
	Operation         string    `json:"operation"`
	GroupID           int64     `json:"group_id"`
	RequestedTokens   int64     `json:"requested_tokens"`
	QuotaDelta        float64   `json:"quota_delta"`
	TargetKeyHash     string    `json:"-"`
	State             string    `json:"state"`
	FulfilledAPIKeyID *int64    `json:"fulfilled_api_key_id,omitempty"`
	ExpiresAt         time.Time `json:"expires_at"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
	Idempotent        bool      `json:"idempotent"`
}

type APIKeySaleReserveInput struct {
	ExternalReference string    `json:"external_reference"`
	Operation         string    `json:"operation"`
	GroupID           int64     `json:"group_id"`
	RequestedTokens   int64     `json:"requested_tokens"`
	QuotaDelta        float64   `json:"-"`
	TargetKey         string    `json:"target_key,omitempty"`
	TargetKeyHash     string    `json:"-"`
	ExpiresAt         time.Time `json:"expires_at"`
}

type APIKeySaleBatchItem struct {
	UserID          int64   `json:"user_id"`
	Name            string  `json:"name"`
	RequestedTokens int64   `json:"requested_tokens"`
	QuotaDelta      float64 `json:"-"`
}

type APIKeySaleBatchFulfillmentInput struct {
	IdempotencyKey string                `json:"-"`
	ReservationID  int64                 `json:"reservation_id"`
	GroupID        int64                 `json:"group_id"`
	Items          []APIKeySaleBatchItem `json:"items"`
}

type APIKeySaleBatchFulfillmentResult struct {
	APIKeys    []*APIKey `json:"api_keys"`
	Idempotent bool      `json:"idempotent"`
}

func (s *adminServiceImpl) AdminLookupAPIKeySale(ctx context.Context, targetKey string) (*APIKey, error) {
	targetKey = strings.TrimSpace(targetKey)
	if len(targetKey) < 16 || len(targetKey) > MaxAPIKeyCredentialBytes {
		return nil, infraerrors.BadRequest("INVALID_TARGET_KEY", "target_key is invalid")
	}
	key, err := s.apiKeyRepo.GetByKey(ctx, targetKey)
	if err != nil || key == nil || key.Quota <= 0 {
		return nil, infraerrors.NotFound("API_KEY_NOT_RENEWABLE", "API key was not found or has unlimited quota")
	}
	return key, nil
}

type APIKeySaleFulfillmentInput struct {
	IdempotencyKey string
	ReservationID  int64
	Legacy         bool
	Operation      string
	UserID         int64
	GroupID        int64
	QuotaDelta     float64
	Name           string
	TargetKey      string
}

type APIKeySaleFulfillmentResult struct {
	APIKey     *APIKey
	Idempotent bool
	Operation  string
}

type apiKeySalesRepository interface {
	GetAPIKeySaleAvailability(ctx context.Context, groupID, tokensPerAccount, tokensPerQuotaUSD int64) (*APIKeySaleAvailability, error)
	ReserveAPIKeySale(ctx context.Context, input APIKeySaleReserveInput, tokensPerAccount, tokensPerQuotaUSD int64) (*APIKeySaleReservation, error)
	ReleaseAPIKeySale(ctx context.Context, reservationID int64) (*APIKeySaleReservation, error)
	FulfillAPIKeySale(ctx context.Context, input APIKeySaleFulfillmentInput, generatedKey string, requestFingerprint string) (*APIKeySaleFulfillmentResult, error)
	FulfillAPIKeySaleBatch(ctx context.Context, input APIKeySaleBatchFulfillmentInput, generatedKeys []string, requestFingerprint string) (*APIKeySaleBatchFulfillmentResult, error)
}

func suggestedSaleTokens(available int64) int64 {
	if available <= 0 {
		return 0
	}
	return available - available%1_000
}

func grokSaleCapacityConfig() (int64, int64, error) {
	perAccount, errAccount := strconv.ParseInt(strings.TrimSpace(os.Getenv(grokTokensPerActiveAccountEnv)), 10, 64)
	perQuota, errQuota := strconv.ParseInt(strings.TrimSpace(os.Getenv(grokTokensPerQuotaUSDEnv)), 10, 64)
	if errAccount != nil || errQuota != nil || perAccount <= 0 || perQuota <= 0 {
		return 0, 0, infraerrors.InternalServer("GROK_CAPACITY_NOT_CONFIGURED", "Grok sales capacity is not configured")
	}
	return perAccount, perQuota, nil
}

func newAPIKeySaleCapacityError(available, minimum int64) error {
	return NewAPIKeySaleCapacityError(available, minimum)
}

func NewAPIKeySaleCapacityError(available, minimum int64) error {
	if available < 0 {
		available = 0
	}
	return infraerrors.Conflict("INSUFFICIENT_GROK_CAPACITY", "requested tokens exceed current Grok capacity").WithMetadata(map[string]string{
		"available_tokens": strconv.FormatInt(available, 10),
		"suggested_tokens": strconv.FormatInt(suggestedSaleTokens(available), 10),
		"minimum_tokens":   strconv.FormatInt(minimum, 10),
	})
}

func quotaForSaleTokens(tokens, tokensPerQuotaUSD int64) float64 {
	return float64(tokens) / float64(tokensPerQuotaUSD)
}

func salesRepository(repo APIKeyRepository) (apiKeySalesRepository, error) {
	salesRepo, ok := repo.(apiKeySalesRepository)
	if !ok {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "API key sales repository is unavailable")
	}
	return salesRepo, nil
}

func (s *adminServiceImpl) AdminGetAPIKeySaleAvailability(ctx context.Context, groupID int64) (*APIKeySaleAvailability, error) {
	if groupID <= 0 {
		return nil, infraerrors.BadRequest("INVALID_GROK_GROUP", "group_id must be positive")
	}
	perAccount, perQuota, err := grokSaleCapacityConfig()
	if err != nil {
		return nil, err
	}
	repo, err := salesRepository(s.apiKeyRepo)
	if err != nil {
		return nil, err
	}
	result, err := repo.GetAPIKeySaleAvailability(ctx, groupID, perAccount, perQuota)
	if err != nil {
		return nil, err
	}
	result.GroupID = groupID
	result.SuggestedTokens = suggestedSaleTokens(result.AvailableTokens)
	result.MinimumTokens = grokSaleMinimumTokens
	result.TokensPerAccount = perAccount
	result.TokensPerQuotaUSD = perQuota
	return result, nil
}

func validateAPIKeySaleReservation(input *APIKeySaleReserveInput, tokensPerQuotaUSD int64) error {
	input.ExternalReference = strings.TrimSpace(input.ExternalReference)
	input.Operation = strings.TrimSpace(input.Operation)
	input.TargetKey = strings.TrimSpace(input.TargetKey)
	if input.ExternalReference == "" || len(input.ExternalReference) > 128 {
		return infraerrors.BadRequest("INVALID_EXTERNAL_REFERENCE", "external_reference is required and must be at most 128 characters")
	}
	if input.Operation != APIKeySaleNew && input.Operation != APIKeySaleRenew && input.Operation != APIKeySaleBatch {
		return infraerrors.BadRequest("INVALID_SALE_OPERATION", "operation must be new_key, renew_key, or batch_new_key")
	}
	if input.GroupID <= 0 || input.RequestedTokens <= 0 {
		return infraerrors.BadRequest("INVALID_RESERVATION", "group_id and requested_tokens must be positive")
	}
	now := time.Now()
	if input.ExpiresAt.Before(now.Add(30*time.Second)) || input.ExpiresAt.After(now.Add(24*time.Hour)) {
		return infraerrors.BadRequest("INVALID_RESERVATION_EXPIRY", "expires_at must be between 30 seconds and 24 hours from now")
	}
	if input.Operation == APIKeySaleRenew {
		if len(input.TargetKey) < 16 || len(input.TargetKey) > MaxAPIKeyCredentialBytes {
			return infraerrors.BadRequest("INVALID_TARGET_KEY", "target_key is invalid")
		}
		digest := sha256.Sum256([]byte(input.TargetKey))
		input.TargetKeyHash = hex.EncodeToString(digest[:])
		input.TargetKey = ""
	} else if input.TargetKey != "" {
		return infraerrors.BadRequest("INVALID_TARGET_KEY", "target_key is only valid for renewals")
	}
	input.QuotaDelta = quotaForSaleTokens(input.RequestedTokens, tokensPerQuotaUSD)
	return nil
}

func (s *adminServiceImpl) AdminReserveAPIKeySale(ctx context.Context, input APIKeySaleReserveInput) (*APIKeySaleReservation, error) {
	perAccount, perQuota, err := grokSaleCapacityConfig()
	if err != nil {
		return nil, err
	}
	if err := validateAPIKeySaleReservation(&input, perQuota); err != nil {
		return nil, err
	}
	repo, err := salesRepository(s.apiKeyRepo)
	if err != nil {
		return nil, err
	}
	return repo.ReserveAPIKeySale(ctx, input, perAccount, perQuota)
}

func (s *adminServiceImpl) AdminReleaseAPIKeySale(ctx context.Context, reservationID int64) (*APIKeySaleReservation, error) {
	if reservationID <= 0 {
		return nil, infraerrors.BadRequest("INVALID_RESERVATION", "reservation_id must be positive")
	}
	repo, err := salesRepository(s.apiKeyRepo)
	if err != nil {
		return nil, err
	}
	return repo.ReleaseAPIKeySale(ctx, reservationID)
}

func apiKeySaleFingerprint(input APIKeySaleFulfillmentInput) string {
	targetHash := sha256.Sum256([]byte(strings.TrimSpace(input.TargetKey)))
	payload, _ := json.Marshal(struct {
		Operation  string  `json:"operation"`
		UserID     int64   `json:"user_id"`
		GroupID    int64   `json:"group_id"`
		QuotaDelta float64 `json:"quota_delta"`
		TargetHash string  `json:"target_hash"`
	}{input.Operation, input.UserID, input.GroupID, input.QuotaDelta, hex.EncodeToString(targetHash[:])})
	digest := sha256.Sum256(payload)
	return hex.EncodeToString(digest[:])
}

func generateSalesAPIKey() (string, error) {
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return "", err
	}
	return "sk-" + hex.EncodeToString(random), nil
}

func validateAPIKeySaleInput(input *APIKeySaleFulfillmentInput) error {
	input.IdempotencyKey = strings.TrimSpace(input.IdempotencyKey)
	input.Operation = strings.TrimSpace(input.Operation)
	input.Name = strings.TrimSpace(input.Name)
	input.TargetKey = strings.TrimSpace(input.TargetKey)
	if input.IdempotencyKey == "" || len(input.IdempotencyKey) > 128 {
		return infraerrors.BadRequest("INVALID_IDEMPOTENCY_KEY", "a valid Idempotency-Key is required")
	}
	if !input.Legacy && input.ReservationID <= 0 {
		return infraerrors.BadRequest("RESERVATION_REQUIRED", "reservation_id is required")
	}
	if input.Operation != APIKeySaleNew && input.Operation != APIKeySaleRenew {
		return infraerrors.BadRequest("INVALID_SALE_OPERATION", "operation must be new_key or renew_key")
	}
	if math.IsNaN(input.QuotaDelta) || math.IsInf(input.QuotaDelta, 0) || input.QuotaDelta <= 0 || input.QuotaDelta > 1_000_000 {
		return infraerrors.BadRequest("INVALID_QUOTA_DELTA", "quota_delta must be finite and positive")
	}
	if input.Operation == APIKeySaleNew {
		if input.UserID <= 0 || input.GroupID <= 0 {
			return infraerrors.BadRequest("INVALID_SALE_OWNER", "user_id and group_id are required for new keys")
		}
		if input.Name == "" {
			input.Name = "Telegram Grok API"
		}
		if len(input.Name) > 100 {
			return infraerrors.BadRequest("INVALID_KEY_NAME", "name is too long")
		}
	} else if len(input.TargetKey) < 16 || len(input.TargetKey) > MaxAPIKeyCredentialBytes {
		return infraerrors.BadRequest("INVALID_TARGET_KEY", "target_key is invalid")
	}
	return nil
}

func apiKeySaleBatchFingerprint(input APIKeySaleBatchFulfillmentInput) string {
	payload, _ := json.Marshal(input)
	digest := sha256.Sum256(payload)
	return hex.EncodeToString(digest[:])
}

func (s *adminServiceImpl) AdminFulfillAPIKeySaleBatch(ctx context.Context, input APIKeySaleBatchFulfillmentInput) (*APIKeySaleBatchFulfillmentResult, error) {
	input.IdempotencyKey = strings.TrimSpace(input.IdempotencyKey)
	if input.IdempotencyKey == "" || len(input.IdempotencyKey) > 128 {
		return nil, infraerrors.BadRequest("INVALID_IDEMPOTENCY_KEY", "a valid Idempotency-Key is required")
	}
	if input.ReservationID <= 0 || input.GroupID <= 0 || len(input.Items) == 0 || len(input.Items) > 1_000 {
		return nil, infraerrors.BadRequest("INVALID_BATCH_FULFILLMENT", "reservation_id, group_id, and one to 1000 items are required")
	}
	_, perQuota, err := grokSaleCapacityConfig()
	if err != nil {
		return nil, err
	}
	generatedKeys := make([]string, len(input.Items))
	for index := range input.Items {
		item := &input.Items[index]
		item.Name = strings.TrimSpace(item.Name)
		if item.UserID <= 0 || item.RequestedTokens <= 0 {
			return nil, infraerrors.BadRequest("INVALID_BATCH_ITEM", "each batch item requires positive user_id and requested_tokens")
		}
		if item.Name == "" {
			item.Name = "Grok API Retail"
		}
		if len(item.Name) > 100 {
			return nil, infraerrors.BadRequest("INVALID_KEY_NAME", "name is too long")
		}
		item.QuotaDelta = quotaForSaleTokens(item.RequestedTokens, perQuota)
		generatedKeys[index], err = generateSalesAPIKey()
		if err != nil {
			return nil, infraerrors.InternalServer("API_KEY_GENERATION_FAILED", "could not generate API key")
		}
	}
	repo, err := salesRepository(s.apiKeyRepo)
	if err != nil {
		return nil, err
	}
	result, err := repo.FulfillAPIKeySaleBatch(ctx, input, generatedKeys, apiKeySaleBatchFingerprint(input))
	if err != nil {
		return nil, err
	}
	if result != nil && s.authCacheInvalidator != nil {
		for _, key := range result.APIKeys {
			if key != nil {
				s.authCacheInvalidator.InvalidateAuthCacheByKey(ctx, key.Key)
			}
		}
	}
	return result, nil
}

func (s *adminServiceImpl) AdminFulfillAPIKeySale(ctx context.Context, input APIKeySaleFulfillmentInput) (*APIKeySaleFulfillmentResult, error) {
	if err := validateAPIKeySaleInput(&input); err != nil {
		return nil, err
	}
	repo, err := salesRepository(s.apiKeyRepo)
	if err != nil {
		return nil, err
	}
	generatedKey := ""
	if input.Operation == APIKeySaleNew {
		var err error
		generatedKey, err = generateSalesAPIKey()
		if err != nil {
			return nil, infraerrors.InternalServer("API_KEY_GENERATION_FAILED", "could not generate API key")
		}
	}
	result, err := repo.FulfillAPIKeySale(ctx, input, generatedKey, apiKeySaleFingerprint(input))
	if err != nil {
		return nil, err
	}
	if result != nil && result.APIKey != nil && s.authCacheInvalidator != nil {
		s.authCacheInvalidator.InvalidateAuthCacheByKey(ctx, result.APIKey.Key)
	}
	return result, nil
}
