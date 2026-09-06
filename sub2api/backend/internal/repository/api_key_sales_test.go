package repository

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func newSalesSQLMock(t *testing.T) (*apiKeyRepository, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New(sqlmock.QueryMatcherOption(sqlmock.QueryMatcherRegexp))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	return &apiKeyRepository{db: db}, mock
}

func expectCapacityQueries(mock sqlmock.Sqlmock, groupID, activeAccounts, outstandingTokens, heldTokens int64) {
	mock.ExpectExec("pg_advisory_xact_lock").WithArgs(groupID).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations").WithArgs(groupID).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(groupID).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("COUNT\\(DISTINCT a.id\\)").WithArgs(groupID).
		WillReturnRows(sqlmock.NewRows([]string{"count"}).AddRow(activeAccounts))
	mock.ExpectQuery("SUM\\(GREATEST\\(k.quota-k.quota_used, 0\\)\\)").WithArgs(groupID, int64(500_000)).
		WillReturnRows(sqlmock.NewRows([]string{"outstanding_tokens"}).AddRow(outstandingTokens))
	mock.ExpectQuery("SUM\\(requested_tokens\\)").WithArgs(groupID).
		WillReturnRows(sqlmock.NewRows([]string{"held_tokens"}).AddRow(heldTokens))
}

func TestAPIKeySaleCapacitySubtractsOutstandingQuotaAndHeldReservations(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	mock.ExpectBegin()
	expectCapacityQueries(mock, 34, 2_000, 93_410_797, 1_000)
	mock.ExpectCommit()

	got, err := repo.GetAPIKeySaleAvailability(context.Background(), 34, 50_000, 500_000)

	require.NoError(t, err)
	require.Equal(t, int64(100_000_000), got.CapacityTokens)
	require.Equal(t, int64(6_588_203), got.AvailableTokens)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleReserveRejectsOneTokenOverExactCapacity(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT id, external_reference.+FROM api_key_sales_reservations.+external_reference=\\$1").
		WithArgs("grok-order-9").WillReturnError(sql.ErrNoRows)
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("COUNT\\(DISTINCT a.id\\)").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"count"}).AddRow(2_000))
	mock.ExpectQuery("SUM\\(GREATEST\\(k.quota-k.quota_used, 0\\)\\)").WithArgs(int64(34), int64(500_000)).
		WillReturnRows(sqlmock.NewRows([]string{"outstanding_tokens"}).AddRow(93_411_797))
	mock.ExpectQuery("SUM\\(requested_tokens\\)").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"held_tokens"}).AddRow(0))
	mock.ExpectRollback()

	_, err := repo.ReserveAPIKeySale(context.Background(), service.APIKeySaleReserveInput{
		ExternalReference: "grok-order-9", Operation: service.APIKeySaleNew, GroupID: 34,
		RequestedTokens: 6_588_204, QuotaDelta: 13.176408, ExpiresAt: time.Now().Add(5 * time.Minute),
	}, 50_000, 500_000)

	require.Equal(t, "INSUFFICIENT_GROK_CAPACITY", infraerrors.Reason(err))
	require.Equal(t, "6588203", infraerrors.FromError(err).Metadata["available_tokens"])
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleReserveAcceptsExactCapacity(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	expires := time.Now().Add(5 * time.Minute).UTC().Truncate(time.Microsecond)
	created := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT id, external_reference.+FROM api_key_sales_reservations.+external_reference=\\$1").
		WithArgs("grok-order-9").WillReturnError(sql.ErrNoRows)
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("COUNT\\(DISTINCT a.id\\)").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"count"}).AddRow(2_000))
	mock.ExpectQuery("SUM\\(GREATEST\\(k.quota-k.quota_used, 0\\)\\)").WithArgs(int64(34), int64(500_000)).
		WillReturnRows(sqlmock.NewRows([]string{"outstanding_tokens"}).AddRow(93_411_797))
	mock.ExpectQuery("SUM\\(requested_tokens\\)").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"held_tokens"}).AddRow(0))
	mock.ExpectQuery("INSERT INTO api_key_sales_reservations").
		WithArgs("grok-order-9", service.APIKeySaleNew, int64(34), int64(6_588_203), 13.176406, "", expires).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(81, "grok-order-9", service.APIKeySaleNew, 34, 6_588_203, 13.176406, "", service.APIKeySaleReservationHeld, nil, expires, created, created))
	mock.ExpectCommit()

	got, err := repo.ReserveAPIKeySale(context.Background(), service.APIKeySaleReserveInput{
		ExternalReference: "grok-order-9", Operation: service.APIKeySaleNew, GroupID: 34,
		RequestedTokens: 6_588_203, QuotaDelta: 13.176406, ExpiresAt: expires,
	}, 50_000, 500_000)

	require.NoError(t, err)
	require.Equal(t, int64(81), got.ID)
	require.Equal(t, service.APIKeySaleReservationHeld, got.State)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleReserveReplaysMatchingExternalReference(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	expires := time.Now().Add(5 * time.Minute).UTC().Truncate(time.Microsecond)
	created := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations").WithArgs(int64(34)).WillReturnResult(sqlmock.NewResult(0, 0))
	mock.ExpectQuery("SELECT id, external_reference.+FROM api_key_sales_reservations.+external_reference=\\$1").
		WithArgs("grok-order-9").WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
		AddRow(81, "grok-order-9", service.APIKeySaleNew, 34, 1_000_000, 2.0, "", service.APIKeySaleReservationHeld, nil, expires, created, created))
	mock.ExpectCommit()

	got, err := repo.ReserveAPIKeySale(context.Background(), service.APIKeySaleReserveInput{
		ExternalReference: "grok-order-9", Operation: service.APIKeySaleNew, GroupID: 34,
		RequestedTokens: 1_000_000, QuotaDelta: 2, ExpiresAt: expires,
	}, 50_000, 500_000)

	require.NoError(t, err)
	require.True(t, got.Idempotent)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleReleaseIsIdempotent(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectQuery("SELECT id, external_reference.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(81)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(81, "grok-order-9", service.APIKeySaleNew, 34, 1_000_000, 2.0, "", service.APIKeySaleReservationReleased, nil, now, now, now))
	mock.ExpectCommit()

	got, err := repo.ReleaseAPIKeySale(context.Background(), 81)

	require.NoError(t, err)
	require.Equal(t, service.APIKeySaleReservationReleased, got.State)
	require.True(t, got.Idempotent)
	require.NoError(t, mock.ExpectationsWereMet())
}

func saleAPIKeyRows(now time.Time, id int64, key, name string, userID, groupID int64, quota float64) *sqlmock.Rows {
	return sqlmock.NewRows([]string{"id", "user_id", "key", "name", "group_id", "status", "quota", "quota_used", "created_at", "updated_at"}).
		AddRow(id, userID, key, name, groupID, service.StatusActive, quota, 0.0, now, now)
}

func TestAPIKeySaleFulfillConsumesHeldReservation(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	expires := now.Add(5 * time.Minute)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("order-81").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_fulfillments").WithArgs("order-81").WillReturnError(sql.ErrNoRows)
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(81)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(81, "grok-order-81", service.APIKeySaleNew, 34, 1_000_000, 2.0, "", service.APIKeySaleReservationHeld, nil, expires, now, now))
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("INSERT INTO api_keys").
		WithArgs(int64(7), "sk-generated", "Telegram Grok API", int64(34), service.StatusActive, 2.0).
		WillReturnRows(saleAPIKeyRows(now, 901, "sk-generated", "Telegram Grok API", 7, 34, 2))
	mock.ExpectExec("INSERT INTO api_key_sales_fulfillments").
		WithArgs("order-81", service.APIKeySaleNew, "fingerprint", int64(901), 2.0).
		WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations.+state='completed'").
		WithArgs(int64(81), int64(901)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()

	got, err := repo.FulfillAPIKeySale(context.Background(), service.APIKeySaleFulfillmentInput{
		IdempotencyKey: "order-81", ReservationID: 81, Operation: service.APIKeySaleNew,
		UserID: 7, GroupID: 34, QuotaDelta: 2, Name: "Telegram Grok API",
	}, "sk-generated", "fingerprint")

	require.NoError(t, err)
	require.Equal(t, int64(901), got.APIKey.ID)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchRollsBackEveryKeyOnItemFailure(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	expires := now.Add(5 * time.Minute)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("batch-81").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(81)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(81, "console-batch-81", service.APIKeySaleBatch, 34, 2_000_000, 4.0, "", service.APIKeySaleReservationHeld, nil, expires, now, now))
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("INSERT INTO api_keys").WillReturnRows(saleAPIKeyRows(now, 901, "sk-one", "one", 7, 34, 2))
	mock.ExpectExec("INSERT INTO api_key_sales_reservation_items").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectQuery("INSERT INTO api_keys").WillReturnError(sql.ErrConnDone)
	mock.ExpectRollback()

	_, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "batch-81", ReservationID: 81, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{
			{UserID: 7, Name: "one", RequestedTokens: 1_000_000, QuotaDelta: 2},
			{UserID: 7, Name: "two", RequestedTokens: 1_000_000, QuotaDelta: 2},
		},
	}, []string{"sk-one", "sk-two"}, "fingerprint")

	require.Error(t, err)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchAcceptsSingleNewKeyReservation(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	expires := now.Add(5 * time.Minute)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("single-new-key").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(84)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(84, "grok-order-84", service.APIKeySaleNew, 34, 500_000, 1.0, "", service.APIKeySaleReservationHeld, nil, expires, now, now))
	mock.ExpectQuery("SELECT platform, status FROM groups").WithArgs(int64(34)).
		WillReturnRows(sqlmock.NewRows([]string{"platform", "status"}).AddRow(service.PlatformGrok, service.StatusActive))
	mock.ExpectQuery("INSERT INTO api_keys").
		WithArgs(int64(7), "sk-single", "Grok API Retail", int64(34), service.StatusActive, 1.0).
		WillReturnRows(saleAPIKeyRows(now, 903, "sk-single", "Grok API Retail", 7, 34, 1))
	mock.ExpectExec("INSERT INTO api_key_sales_reservation_items").
		WithArgs(int64(84), 0, int64(500_000), 1.0, int64(903)).WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("UPDATE api_key_sales_reservations.+state='completed'").
		WithArgs(int64(84)).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()

	got, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "single-new-key", ReservationID: 84, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{{UserID: 7, Name: "Grok API Retail", RequestedTokens: 500_000, QuotaDelta: 1}},
	}, []string{"sk-single"}, "fingerprint")

	require.NoError(t, err)
	require.Len(t, got.APIKeys, 1)
	require.Equal(t, int64(903), got.APIKeys[0].ID)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchRejectsMultiItemNewKeyReservation(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("multi-new-key").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(85)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(85, "grok-order-85", service.APIKeySaleNew, 34, 1_000_000, 2.0, "", service.APIKeySaleReservationHeld, nil, now.Add(5*time.Minute), now, now))
	mock.ExpectRollback()

	_, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "multi-new-key", ReservationID: 85, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{
			{UserID: 7, Name: "one", RequestedTokens: 500_000, QuotaDelta: 1},
			{UserID: 7, Name: "two", RequestedTokens: 500_000, QuotaDelta: 1},
		},
	}, []string{"sk-one", "sk-two"}, "fingerprint")

	require.Equal(t, "RESERVATION_MISMATCH", infraerrors.Reason(err))
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchRejectsSingleNewKeyTokenMismatch(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("single-new-key-mismatch").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(86)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(86, "grok-order-86", service.APIKeySaleNew, 34, 500_000, 1.0, "", service.APIKeySaleReservationHeld, nil, now.Add(5*time.Minute), now, now))
	mock.ExpectRollback()

	_, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "single-new-key-mismatch", ReservationID: 86, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{{UserID: 7, Name: "Grok API Retail", RequestedTokens: 1_000_000, QuotaDelta: 2}},
	}, []string{"sk-single"}, "fingerprint")

	require.Equal(t, "RESERVATION_MISMATCH", infraerrors.Reason(err))
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchReplaysCompletedSingleNewKeyReservation(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("single-new-key-replay").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(87)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(87, "grok-order-87", service.APIKeySaleNew, 34, 500_000, 1.0, "", service.APIKeySaleReservationCompleted, nil, now.Add(5*time.Minute), now, now))
	mock.ExpectQuery("FROM api_key_sales_reservation_items i").WithArgs(int64(87)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "user_id", "key", "name", "group_id", "status", "quota", "quota_used", "created_at", "updated_at"}).
			AddRow(904, 7, "sk-single", "Grok API Retail", 34, service.StatusActive, 1.0, 0.0, now, now))
	mock.ExpectCommit()

	got, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "single-new-key-replay", ReservationID: 87, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{{UserID: 7, Name: "Grok API Retail", RequestedTokens: 500_000, QuotaDelta: 1}},
	}, []string{"unused"}, "fingerprint")

	require.NoError(t, err)
	require.True(t, got.Idempotent)
	require.Len(t, got.APIKeys, 1)
	require.Equal(t, int64(904), got.APIKeys[0].ID)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchRejectsGroupMismatchBeforeCreatingKeys(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("batch-wrong-group").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(82)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(82, "console-batch-82", service.APIKeySaleBatch, 2, 1_000_000, 2.0, "", service.APIKeySaleReservationHeld, nil, now.Add(5*time.Minute), now, now))
	mock.ExpectRollback()

	_, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "batch-wrong-group", ReservationID: 82, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{{UserID: 7, Name: "one", RequestedTokens: 1_000_000, QuotaDelta: 2}},
	}, []string{"sk-one"}, "fingerprint")

	require.Equal(t, "RESERVATION_MISMATCH", infraerrors.Reason(err))
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestAPIKeySaleFulfillBatchReplayReturnsOriginalKeys(t *testing.T) {
	repo, mock := newSalesSQLMock(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	mock.ExpectBegin()
	mock.ExpectExec("pg_advisory_xact_lock\\(hashtextextended").WithArgs("batch-replay").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("FROM api_key_sales_reservations.+WHERE id=\\$1 FOR UPDATE").WithArgs(int64(83)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "external_reference", "operation", "group_id", "requested_tokens", "quota_delta", "target_key_hash", "state", "fulfilled_api_key_id", "expires_at", "created_at", "updated_at"}).
			AddRow(83, "console-batch-83", service.APIKeySaleBatch, 34, 2_000_000, 4.0, "", service.APIKeySaleReservationCompleted, nil, now.Add(5*time.Minute), now, now))
	mock.ExpectQuery("FROM api_key_sales_reservation_items i").WithArgs(int64(83)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "user_id", "key", "name", "group_id", "status", "quota", "quota_used", "created_at", "updated_at"}).
			AddRow(901, 7, "sk-one", "one", 34, service.StatusActive, 2.0, 0.0, now, now).
			AddRow(902, 7, "sk-two", "two", 34, service.StatusActive, 2.0, 0.0, now, now))
	mock.ExpectCommit()

	got, err := repo.FulfillAPIKeySaleBatch(context.Background(), service.APIKeySaleBatchFulfillmentInput{
		IdempotencyKey: "batch-replay", ReservationID: 83, GroupID: 34,
		Items: []service.APIKeySaleBatchItem{
			{UserID: 7, Name: "one", RequestedTokens: 1_000_000, QuotaDelta: 2},
			{UserID: 7, Name: "two", RequestedTokens: 1_000_000, QuotaDelta: 2},
		},
	}, []string{"unused-one", "unused-two"}, "fingerprint")

	require.NoError(t, err)
	require.True(t, got.Idempotent)
	require.Equal(t, []int64{901, 902}, []int64{got.APIKeys[0].ID, got.APIKeys[1].ID})
	require.NoError(t, mock.ExpectationsWereMet())
}
