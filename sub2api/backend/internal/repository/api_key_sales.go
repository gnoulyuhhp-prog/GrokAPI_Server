package repository

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

type apiKeySaleRowScanner interface {
	Scan(dest ...any) error
}

func scanAPIKeySaleReservation(row apiKeySaleRowScanner) (*service.APIKeySaleReservation, error) {
	reservation := &service.APIKeySaleReservation{}
	var fulfilledID sql.NullInt64
	err := row.Scan(
		&reservation.ID,
		&reservation.ExternalReference,
		&reservation.Operation,
		&reservation.GroupID,
		&reservation.RequestedTokens,
		&reservation.QuotaDelta,
		&reservation.TargetKeyHash,
		&reservation.State,
		&fulfilledID,
		&reservation.ExpiresAt,
		&reservation.CreatedAt,
		&reservation.UpdatedAt,
	)
	if fulfilledID.Valid {
		reservation.FulfilledAPIKeyID = &fulfilledID.Int64
	}
	return reservation, err
}

const apiKeySaleReservationColumns = `
	id, external_reference, operation, group_id, requested_tokens, quota_delta,
	target_key_hash, state, fulfilled_api_key_id, expires_at, created_at, updated_at`

func lockAPIKeySaleGroup(ctx context.Context, tx *sql.Tx, groupID int64) error {
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, groupID); err != nil {
		return fmt.Errorf("lock API key sale group: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE api_key_sales_reservations
		SET state='expired', updated_at=NOW()
		WHERE group_id=$1 AND state='held' AND expires_at<=NOW()
	`, groupID); err != nil {
		return fmt.Errorf("expire API key sale reservations: %w", err)
	}
	return nil
}

func calculateAPIKeySaleAvailability(ctx context.Context, tx *sql.Tx, groupID, tokensPerAccount, tokensPerQuotaUSD int64) (*service.APIKeySaleAvailability, error) {
	var platform, status string
	if err := tx.QueryRowContext(ctx, `SELECT platform, status FROM groups WHERE id=$1 AND deleted_at IS NULL`, groupID).Scan(&platform, &status); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, infraerrors.BadRequest("GROK_GROUP_NOT_FOUND", "target Grok group was not found")
		}
		return nil, fmt.Errorf("load Grok capacity group: %w", err)
	}
	if platform != service.PlatformGrok || status != service.StatusActive {
		return nil, infraerrors.BadRequest("INVALID_GROK_GROUP", "target group must be an active Grok group")
	}

	var activeAccounts, outstandingTokens, reservedTokens int64
	if err := tx.QueryRowContext(ctx, `
		SELECT COUNT(DISTINCT a.id)
		FROM accounts a
		JOIN account_groups ag ON ag.account_id=a.id
		WHERE ag.group_id=$1 AND a.status='active' AND a.deleted_at IS NULL
	`, groupID).Scan(&activeAccounts); err != nil {
		return nil, fmt.Errorf("count active Grok accounts: %w", err)
	}
	if err := tx.QueryRowContext(ctx, `
		SELECT COALESCE(FLOOR(SUM(GREATEST(k.quota-k.quota_used, 0))*$2), 0)::BIGINT
		FROM api_keys k
		WHERE k.group_id=$1 AND k.deleted_at IS NULL AND k.quota>0
	`, groupID, tokensPerQuotaUSD).Scan(&outstandingTokens); err != nil {
		return nil, fmt.Errorf("sum outstanding Grok key quota: %w", err)
	}
	if err := tx.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(requested_tokens), 0)::BIGINT
		FROM api_key_sales_reservations
		WHERE group_id=$1 AND state='held' AND expires_at>NOW()
	`, groupID).Scan(&reservedTokens); err != nil {
		return nil, fmt.Errorf("sum held Grok reservations: %w", err)
	}
	if activeAccounts > math.MaxInt64/tokensPerAccount {
		return nil, infraerrors.InternalServer("GROK_CAPACITY_OVERFLOW", "Grok sales capacity is too large")
	}
	capacity := activeAccounts * tokensPerAccount
	available := capacity - outstandingTokens - reservedTokens
	if available < 0 {
		available = 0
	}
	return &service.APIKeySaleAvailability{
		GroupID:           groupID,
		ActiveAccounts:    activeAccounts,
		CapacityTokens:    capacity,
		OutstandingTokens: outstandingTokens,
		ReservedTokens:    reservedTokens,
		AvailableTokens:   available,
	}, nil
}

func (r *apiKeyRepository) GetAPIKeySaleAvailability(ctx context.Context, groupID, tokensPerAccount, tokensPerQuotaUSD int64) (*service.APIKeySaleAvailability, error) {
	if r.db == nil {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "database transaction support is unavailable")
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable, ReadOnly: false})
	if err != nil {
		return nil, fmt.Errorf("begin API key sale availability: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if err := lockAPIKeySaleGroup(ctx, tx, groupID); err != nil {
		return nil, err
	}
	result, err := calculateAPIKeySaleAvailability(ctx, tx, groupID, tokensPerAccount, tokensPerQuotaUSD)
	if err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit API key sale availability: %w", err)
	}
	return result, nil
}

func reservationMatchesInput(reservation *service.APIKeySaleReservation, input service.APIKeySaleReserveInput) bool {
	return reservation.Operation == input.Operation &&
		reservation.GroupID == input.GroupID &&
		reservation.RequestedTokens == input.RequestedTokens &&
		math.Abs(reservation.QuotaDelta-input.QuotaDelta) < 0.00000001 &&
		reservation.TargetKeyHash == input.TargetKeyHash
}

func (r *apiKeyRepository) ReserveAPIKeySale(ctx context.Context, input service.APIKeySaleReserveInput, tokensPerAccount, tokensPerQuotaUSD int64) (*service.APIKeySaleReservation, error) {
	if r.db == nil {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "database transaction support is unavailable")
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return nil, fmt.Errorf("begin API key sale reservation: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if err := lockAPIKeySaleGroup(ctx, tx, input.GroupID); err != nil {
		return nil, err
	}
	existing, lookupErr := scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
		SELECT `+apiKeySaleReservationColumns+`
		FROM api_key_sales_reservations
		WHERE external_reference=$1
	`, input.ExternalReference))
	if lookupErr == nil {
		if !reservationMatchesInput(existing, input) {
			return nil, infraerrors.Conflict("RESERVATION_REFERENCE_CONFLICT", "external_reference was already used for different sale data")
		}
		existing.Idempotent = true
		if err := tx.Commit(); err != nil {
			return nil, fmt.Errorf("commit replayed API key sale reservation: %w", err)
		}
		return existing, nil
	}
	if !errors.Is(lookupErr, sql.ErrNoRows) {
		return nil, fmt.Errorf("lookup API key sale reservation: %w", lookupErr)
	}
	availability, err := calculateAPIKeySaleAvailability(ctx, tx, input.GroupID, tokensPerAccount, tokensPerQuotaUSD)
	if err != nil {
		return nil, err
	}
	if input.RequestedTokens > availability.AvailableTokens {
		return nil, service.NewAPIKeySaleCapacityError(availability.AvailableTokens, 1_000)
	}
	reservation, err := scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
		INSERT INTO api_key_sales_reservations
		(external_reference, operation, group_id, requested_tokens, quota_delta, target_key_hash, state, expires_at)
		VALUES ($1, $2, $3, $4, $5, $6, 'held', $7)
		RETURNING `+apiKeySaleReservationColumns,
		input.ExternalReference, input.Operation, input.GroupID, input.RequestedTokens,
		input.QuotaDelta, input.TargetKeyHash, input.ExpiresAt,
	))
	if err != nil {
		return nil, fmt.Errorf("insert API key sale reservation: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit API key sale reservation: %w", err)
	}
	return reservation, nil
}

func (r *apiKeyRepository) ReleaseAPIKeySale(ctx context.Context, reservationID int64) (*service.APIKeySaleReservation, error) {
	if r.db == nil {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "database transaction support is unavailable")
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return nil, fmt.Errorf("begin API key sale release: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	reservation, err := scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
		SELECT `+apiKeySaleReservationColumns+`
		FROM api_key_sales_reservations
		WHERE id=$1 FOR UPDATE
	`, reservationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, infraerrors.NotFound("RESERVATION_NOT_FOUND", "Grok capacity reservation was not found")
	}
	if err != nil {
		return nil, fmt.Errorf("lock API key sale reservation: %w", err)
	}
	switch reservation.State {
	case service.APIKeySaleReservationHeld:
		if reservation.ExpiresAt.Before(time.Now()) {
			reservation.State = service.APIKeySaleReservationExpired
		} else {
			reservation.State = service.APIKeySaleReservationReleased
		}
		reservation, err = scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
			UPDATE api_key_sales_reservations
			SET state=$2, updated_at=NOW()
			WHERE id=$1 AND state='held'
			RETURNING `+apiKeySaleReservationColumns,
			reservationID, reservation.State,
		))
		if err != nil {
			return nil, fmt.Errorf("release API key sale reservation: %w", err)
		}
	case service.APIKeySaleReservationReleased, service.APIKeySaleReservationExpired:
		reservation.Idempotent = true
	case service.APIKeySaleReservationCompleted:
		return nil, infraerrors.Conflict("RESERVATION_ALREADY_COMPLETED", "completed Grok capacity cannot be released")
	default:
		return nil, infraerrors.Conflict("INVALID_RESERVATION_STATE", "Grok capacity reservation has an invalid state")
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit API key sale release: %w", err)
	}
	return reservation, nil
}

func scanSaleAPIKey(row *sql.Row) (*service.APIKey, error) {
	key := &service.APIKey{}
	err := row.Scan(&key.ID, &key.UserID, &key.Key, &key.Name, &key.GroupID, &key.Status, &key.Quota, &key.QuotaUsed, &key.CreatedAt, &key.UpdatedAt)
	return key, err
}

func validateFulfillmentReservation(reservation *service.APIKeySaleReservation, input service.APIKeySaleFulfillmentInput) error {
	if reservation.State != service.APIKeySaleReservationHeld {
		return infraerrors.Conflict("RESERVATION_NOT_HELD", "Grok capacity reservation is not held")
	}
	if !reservation.ExpiresAt.After(time.Now()) {
		return infraerrors.Conflict("RESERVATION_EXPIRED", "Grok capacity reservation has expired")
	}
	if reservation.Operation != input.Operation || reservation.GroupID != input.GroupID || math.Abs(reservation.QuotaDelta-input.QuotaDelta) >= 0.00000001 {
		return infraerrors.Conflict("RESERVATION_MISMATCH", "Grok capacity reservation does not match fulfillment")
	}
	if input.Operation == service.APIKeySaleRenew {
		digest := sha256.Sum256([]byte(input.TargetKey))
		if reservation.TargetKeyHash != hex.EncodeToString(digest[:]) {
			return infraerrors.Conflict("RESERVATION_MISMATCH", "Grok capacity reservation does not match renewal target")
		}
	}
	return nil
}

func (r *apiKeyRepository) FulfillAPIKeySale(ctx context.Context, input service.APIKeySaleFulfillmentInput, generatedKey string, requestFingerprint string) (*service.APIKeySaleFulfillmentResult, error) {
	if r.db == nil {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "database transaction support is unavailable")
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return nil, fmt.Errorf("begin API key sale: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	// Serialize requests that carry the same idempotency key. Without this lock,
	// two concurrent webhook retries can both pass the lookup and one would fail
	// at the unique insert after doing otherwise valid work. The transaction lock
	// makes the second request observe and return the committed fulfillment.
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, input.IdempotencyKey); err != nil {
		return nil, fmt.Errorf("lock API key sale: %w", err)
	}

	var existingOperation, existingFingerprint string
	var existingKeyID int64
	err = tx.QueryRowContext(ctx, `
		SELECT operation, request_fingerprint, api_key_id
		FROM api_key_sales_fulfillments
		WHERE idempotency_key=$1
	`, input.IdempotencyKey).Scan(&existingOperation, &existingFingerprint, &existingKeyID)
	if err == nil {
		if existingOperation != input.Operation || existingFingerprint != requestFingerprint {
			return nil, infraerrors.Conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with different sale data")
		}
		key, loadErr := scanSaleAPIKey(tx.QueryRowContext(ctx, `
			SELECT id, user_id, key, name, group_id, status, quota, quota_used, created_at, updated_at
			FROM api_keys WHERE id=$1 AND deleted_at IS NULL
		`, existingKeyID))
		if loadErr != nil {
			return nil, fmt.Errorf("reload fulfilled API key: %w", loadErr)
		}
		return &service.APIKeySaleFulfillmentResult{APIKey: key, Idempotent: true, Operation: input.Operation}, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("lookup API key sale: %w", err)
	}
	if !input.Legacy {
		reservation, reservationErr := scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
			SELECT `+apiKeySaleReservationColumns+`
			FROM api_key_sales_reservations
			WHERE id=$1 FOR UPDATE
		`, input.ReservationID))
		if errors.Is(reservationErr, sql.ErrNoRows) {
			return nil, infraerrors.NotFound("RESERVATION_NOT_FOUND", "Grok capacity reservation was not found")
		}
		if reservationErr != nil {
			return nil, fmt.Errorf("lock fulfillment reservation: %w", reservationErr)
		}
		if err := validateFulfillmentReservation(reservation, input); err != nil {
			return nil, err
		}
	}

	var key *service.APIKey
	if input.Operation == service.APIKeySaleNew {
		var groupPlatform, groupStatus string
		if err := tx.QueryRowContext(ctx, "SELECT platform, status FROM groups WHERE id=$1", input.GroupID).Scan(&groupPlatform, &groupStatus); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return nil, infraerrors.BadRequest("GROK_GROUP_NOT_FOUND", "target Grok group was not found")
			}
			return nil, fmt.Errorf("load target group: %w", err)
		}
		if groupPlatform != service.PlatformGrok || groupStatus != service.StatusActive {
			return nil, infraerrors.BadRequest("INVALID_GROK_GROUP", "target group must be an active Grok group")
		}
		key, err = scanSaleAPIKey(tx.QueryRowContext(ctx, `
			INSERT INTO api_keys (user_id, key, name, group_id, status, quota, quota_used, ip_whitelist, ip_blacklist, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, 0, '[]'::jsonb, '[]'::jsonb, NOW(), NOW())
			RETURNING id, user_id, key, name, group_id, status, quota, quota_used, created_at, updated_at
		`, input.UserID, generatedKey, input.Name, input.GroupID, service.StatusActive, input.QuotaDelta))
		if err != nil {
			return nil, fmt.Errorf("create sold API key: %w", err)
		}
	} else {
		key, err = scanSaleAPIKey(tx.QueryRowContext(ctx, `
			UPDATE api_keys
			SET quota=quota+$1,
			    status=CASE WHEN status=$2 THEN $3 ELSE status END,
			    updated_at=NOW()
			WHERE key=$4 AND deleted_at IS NULL AND quota>0
			RETURNING id, user_id, key, name, group_id, status, quota, quota_used, created_at, updated_at
		`, input.QuotaDelta, service.StatusAPIKeyQuotaExhausted, service.StatusActive, input.TargetKey))
		if errors.Is(err, sql.ErrNoRows) {
			return nil, infraerrors.NotFound("API_KEY_NOT_RENEWABLE", "API key was not found or has unlimited quota")
		}
		if err != nil {
			return nil, fmt.Errorf("renew API key quota: %w", err)
		}
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO api_key_sales_fulfillments
		(idempotency_key, operation, request_fingerprint, api_key_id, quota_delta)
		VALUES ($1, $2, $3, $4, $5)
	`, input.IdempotencyKey, input.Operation, requestFingerprint, key.ID, input.QuotaDelta); err != nil {
		return nil, fmt.Errorf("record API key sale: %w", err)
	}
	if !input.Legacy {
		result, updateErr := tx.ExecContext(ctx, `
			UPDATE api_key_sales_reservations
			SET state='completed', fulfilled_api_key_id=$2, updated_at=NOW()
			WHERE id=$1 AND state='held'
		`, input.ReservationID, key.ID)
		if updateErr != nil {
			return nil, fmt.Errorf("complete API key sale reservation: %w", updateErr)
		}
		if affected, affectedErr := result.RowsAffected(); affectedErr != nil || affected != 1 {
			return nil, infraerrors.Conflict("RESERVATION_NOT_HELD", "Grok capacity reservation is no longer held")
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit API key sale: %w", err)
	}
	return &service.APIKeySaleFulfillmentResult{APIKey: key, Operation: input.Operation}, nil
}

func (r *apiKeyRepository) FulfillAPIKeySaleBatch(ctx context.Context, input service.APIKeySaleBatchFulfillmentInput, generatedKeys []string, requestFingerprint string) (*service.APIKeySaleBatchFulfillmentResult, error) {
	if r.db == nil {
		return nil, infraerrors.InternalServer("API_KEY_SALES_UNAVAILABLE", "database transaction support is unavailable")
	}
	if len(input.Items) != len(generatedKeys) {
		return nil, infraerrors.BadRequest("INVALID_BATCH_FULFILLMENT", "generated key count does not match batch items")
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return nil, fmt.Errorf("begin batch API key sale: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, input.IdempotencyKey); err != nil {
		return nil, fmt.Errorf("lock batch API key sale: %w", err)
	}
	reservation, err := scanAPIKeySaleReservation(tx.QueryRowContext(ctx, `
		SELECT `+apiKeySaleReservationColumns+`
		FROM api_key_sales_reservations
		WHERE id=$1 FOR UPDATE
	`, input.ReservationID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, infraerrors.NotFound("RESERVATION_NOT_FOUND", "Grok capacity reservation was not found")
	}
	if err != nil {
		return nil, fmt.Errorf("lock batch fulfillment reservation: %w", err)
	}
	if reservation.State == service.APIKeySaleReservationCompleted {
		rows, loadErr := tx.QueryContext(ctx, `
			SELECT k.id, k.user_id, k.key, k.name, k.group_id, k.status, k.quota, k.quota_used, k.created_at, k.updated_at
			FROM api_key_sales_reservation_items i
			JOIN api_keys k ON k.id=i.api_key_id
			WHERE i.reservation_id=$1
			ORDER BY i.item_index
		`, input.ReservationID)
		if loadErr != nil {
			return nil, fmt.Errorf("reload batch API keys: %w", loadErr)
		}
		defer rows.Close()
		keys := make([]*service.APIKey, 0, len(input.Items))
		for rows.Next() {
			key := &service.APIKey{}
			if scanErr := rows.Scan(&key.ID, &key.UserID, &key.Key, &key.Name, &key.GroupID, &key.Status, &key.Quota, &key.QuotaUsed, &key.CreatedAt, &key.UpdatedAt); scanErr != nil {
				return nil, fmt.Errorf("scan replayed batch API key: %w", scanErr)
			}
			keys = append(keys, key)
		}
		if rowsErr := rows.Err(); rowsErr != nil {
			return nil, fmt.Errorf("read replayed batch API keys: %w", rowsErr)
		}
		if len(keys) != len(input.Items) {
			return nil, infraerrors.Conflict("IDEMPOTENCY_CONFLICT", "batch reservation was completed with different items")
		}
		for index, key := range keys {
			item := input.Items[index]
			if key.UserID != item.UserID || key.Name != item.Name || key.GroupID == nil || *key.GroupID != input.GroupID || math.Abs(key.Quota-item.QuotaDelta) >= 0.00000001 {
				return nil, infraerrors.Conflict("IDEMPOTENCY_CONFLICT", "batch reservation was completed with different items")
			}
		}
		if err := tx.Commit(); err != nil {
			return nil, fmt.Errorf("commit replayed batch API key sale: %w", err)
		}
		return &service.APIKeySaleBatchFulfillmentResult{APIKeys: keys, Idempotent: true}, nil
	}
	if reservation.State != service.APIKeySaleReservationHeld || !reservation.ExpiresAt.After(time.Now()) {
		return nil, infraerrors.Conflict("RESERVATION_NOT_HELD", "batch Grok capacity reservation is not held")
	}
	var requestedTokens int64
	var quotaDelta float64
	for _, item := range input.Items {
		if item.RequestedTokens > math.MaxInt64-requestedTokens {
			return nil, infraerrors.BadRequest("INVALID_BATCH_FULFILLMENT", "batch token total is too large")
		}
		requestedTokens += item.RequestedTokens
		quotaDelta += item.QuotaDelta
	}
	compatibleOperation := reservation.Operation == service.APIKeySaleBatch ||
		(reservation.Operation == service.APIKeySaleNew && len(input.Items) == 1)
	if !compatibleOperation || reservation.GroupID != input.GroupID || reservation.RequestedTokens != requestedTokens || math.Abs(reservation.QuotaDelta-quotaDelta) >= 0.00000001 {
		return nil, infraerrors.Conflict("RESERVATION_MISMATCH", "batch reservation does not match fulfillment")
	}
	var groupPlatform, groupStatus string
	if err := tx.QueryRowContext(ctx, "SELECT platform, status FROM groups WHERE id=$1", input.GroupID).Scan(&groupPlatform, &groupStatus); err != nil {
		return nil, fmt.Errorf("load batch target group: %w", err)
	}
	if groupPlatform != service.PlatformGrok || groupStatus != service.StatusActive {
		return nil, infraerrors.BadRequest("INVALID_GROK_GROUP", "target group must be an active Grok group")
	}
	keys := make([]*service.APIKey, 0, len(input.Items))
	for index, item := range input.Items {
		key, createErr := scanSaleAPIKey(tx.QueryRowContext(ctx, `
			INSERT INTO api_keys (user_id, key, name, group_id, status, quota, quota_used, ip_whitelist, ip_blacklist, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, 0, '[]'::jsonb, '[]'::jsonb, NOW(), NOW())
			RETURNING id, user_id, key, name, group_id, status, quota, quota_used, created_at, updated_at
		`, item.UserID, generatedKeys[index], item.Name, input.GroupID, service.StatusActive, item.QuotaDelta))
		if createErr != nil {
			return nil, fmt.Errorf("create batch sold API key %d: %w", index, createErr)
		}
		if _, itemErr := tx.ExecContext(ctx, `
			INSERT INTO api_key_sales_reservation_items
			(reservation_id, item_index, requested_tokens, quota_delta, api_key_id)
			VALUES ($1, $2, $3, $4, $5)
		`, input.ReservationID, index, item.RequestedTokens, item.QuotaDelta, key.ID); itemErr != nil {
			return nil, fmt.Errorf("record batch sold API key %d: %w", index, itemErr)
		}
		keys = append(keys, key)
	}
	result, err := tx.ExecContext(ctx, `
		UPDATE api_key_sales_reservations
		SET state='completed', updated_at=NOW()
		WHERE id=$1 AND state='held'
	`, input.ReservationID)
	if err != nil {
		return nil, fmt.Errorf("complete batch API key sale reservation: %w", err)
	}
	if affected, affectedErr := result.RowsAffected(); affectedErr != nil || affected != 1 {
		return nil, infraerrors.Conflict("RESERVATION_NOT_HELD", "batch Grok capacity reservation is no longer held")
	}
	_ = requestFingerprint // reservation contents and created item rows form the replay fingerprint.
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit batch API key sale: %w", err)
	}
	return &service.APIKeySaleBatchFulfillmentResult{APIKeys: keys}, nil
}
