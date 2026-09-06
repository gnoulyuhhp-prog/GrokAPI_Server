package service

import (
	"context"
	"testing"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/stretchr/testify/require"
)

type fakeSalesCapacityRepo struct {
	APIKeyRepository
	available int64
	reserved  APIKeySaleReserveInput
}

func (f *fakeSalesCapacityRepo) GetAPIKeySaleAvailability(context.Context, int64, int64, int64) (*APIKeySaleAvailability, error) {
	return &APIKeySaleAvailability{AvailableTokens: f.available}, nil
}

func (f *fakeSalesCapacityRepo) ReserveAPIKeySale(_ context.Context, input APIKeySaleReserveInput, _, _ int64) (*APIKeySaleReservation, error) {
	f.reserved = input
	if input.RequestedTokens > f.available {
		return nil, newAPIKeySaleCapacityError(f.available, 1_000_000)
	}
	return &APIKeySaleReservation{ID: 12, ExternalReference: input.ExternalReference, State: APIKeySaleReservationHeld}, nil
}

func (f *fakeSalesCapacityRepo) ReleaseAPIKeySale(context.Context, int64) (*APIKeySaleReservation, error) {
	return &APIKeySaleReservation{ID: 12, State: APIKeySaleReservationReleased}, nil
}

func (f *fakeSalesCapacityRepo) FulfillAPIKeySale(context.Context, APIKeySaleFulfillmentInput, string, string) (*APIKeySaleFulfillmentResult, error) {
	panic("not used")
}

func (f *fakeSalesCapacityRepo) FulfillAPIKeySaleBatch(context.Context, APIKeySaleBatchFulfillmentInput, []string, string) (*APIKeySaleBatchFulfillmentResult, error) {
	panic("not used")
}

func TestSuggestedTokensRoundsDownToOneThousand(t *testing.T) {
	require.Equal(t, int64(6_588_000), suggestedSaleTokens(6_588_203))
	require.Zero(t, suggestedSaleTokens(999))
}

func TestReserveRejectsMoreThanExactAvailability(t *testing.T) {
	t.Setenv(grokTokensPerActiveAccountEnv, "50000")
	t.Setenv(grokTokensPerQuotaUSDEnv, "500000")
	repo := &fakeSalesCapacityRepo{available: 6_588_203}
	svc := &adminServiceImpl{apiKeyRepo: repo}

	_, err := svc.AdminReserveAPIKeySale(context.Background(), APIKeySaleReserveInput{
		ExternalReference: "grok-order-9",
		Operation:         APIKeySaleNew,
		GroupID:           34,
		RequestedTokens:   6_588_204,
		ExpiresAt:         time.Now().Add(5 * time.Minute),
	})

	require.Equal(t, "INSUFFICIENT_GROK_CAPACITY", infraerrors.Reason(err))
	require.Equal(t, "6588203", infraerrors.FromError(err).Metadata["available_tokens"])
	require.Equal(t, "6588000", infraerrors.FromError(err).Metadata["suggested_tokens"])
}

func TestReserveExactAvailabilityHashesRenewalTarget(t *testing.T) {
	t.Setenv(grokTokensPerActiveAccountEnv, "50000")
	t.Setenv(grokTokensPerQuotaUSDEnv, "500000")
	repo := &fakeSalesCapacityRepo{available: 6_588_203}
	svc := &adminServiceImpl{apiKeyRepo: repo}
	target := "sk-secret-renewal-target-123456"

	result, err := svc.AdminReserveAPIKeySale(context.Background(), APIKeySaleReserveInput{
		ExternalReference: " grok-order-10 ",
		Operation:         APIKeySaleRenew,
		GroupID:           34,
		RequestedTokens:   6_588_203,
		TargetKey:         target,
		ExpiresAt:         time.Now().Add(5 * time.Minute),
	})

	require.NoError(t, err)
	require.Equal(t, int64(12), result.ID)
	require.Equal(t, "grok-order-10", repo.reserved.ExternalReference)
	require.Empty(t, repo.reserved.TargetKey)
	require.Len(t, repo.reserved.TargetKeyHash, 64)
	require.NotContains(t, repo.reserved.TargetKeyHash, target)
	require.InDelta(t, 13.176406, repo.reserved.QuotaDelta, 0.0000001)
}

func TestReserveRequiresCapacityConfiguration(t *testing.T) {
	t.Setenv(grokTokensPerActiveAccountEnv, "")
	t.Setenv(grokTokensPerQuotaUSDEnv, "500000")
	svc := &adminServiceImpl{apiKeyRepo: &fakeSalesCapacityRepo{available: 1_000_000}}

	_, err := svc.AdminReserveAPIKeySale(context.Background(), APIKeySaleReserveInput{
		ExternalReference: "grok-order-11",
		Operation:         APIKeySaleNew,
		GroupID:           34,
		RequestedTokens:   1_000_000,
		ExpiresAt:         time.Now().Add(5 * time.Minute),
	})

	require.Equal(t, "GROK_CAPACITY_NOT_CONFIGURED", infraerrors.Reason(err))
}
