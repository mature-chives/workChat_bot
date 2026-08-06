package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrUserDisabled = errors.New("user is disabled")

type Store struct {
	pool *pgxpool.Pool
}

type InboundText struct {
	TenantID          string
	IntegrationID     string
	ExternalMessageID string
	ExternalUserID    string
	Content           string
	ReceivedAt        time.Time
}

type AcceptedMessage struct {
	RequestID      string
	InputMessageID string
	UserID         string
	ConversationID string
	Queued         bool
	Duplicate      bool
}

type Question struct {
	TenantID       string
	RequestID      string
	InputMessageID string
	UserID         string
	ConversationID string
	Content        string
	Channel        string
}

type OutboundCitation struct {
	Index        int
	Title        string
	LocatorType  string
	LocatorValue string
}

type OutboundAnswer struct {
	MessageID      string
	InternalUserID string
	ExternalUserID string
	Content        string
	Citations      []OutboundCitation
}

func Open(ctx context.Context, databaseURL string) (*Store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("create postgres pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() {
	s.pool.Close()
}

func (s *Store) Ping(ctx context.Context) error {
	return s.pool.Ping(ctx)
}

func (s *Store) AcceptInboundText(ctx context.Context, input InboundText) (AcceptedMessage, error) {
	if existing, found, err := s.findInbound(ctx, input.TenantID, input.IntegrationID, input.ExternalMessageID); err != nil {
		return AcceptedMessage{}, err
	} else if found {
		existing.Duplicate = true
		return existing, nil
	}

	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return AcceptedMessage{}, fmt.Errorf("begin inbound transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var userID string
	var userStatus string
	err = tx.QueryRow(ctx, `
		INSERT INTO users (tenant_id, wecom_user_id, display_name)
		VALUES ($1::uuid, $2, $2)
		ON CONFLICT (tenant_id, wecom_user_id)
		DO UPDATE SET updated_at = now()
		RETURNING id::text, status
	`, input.TenantID, input.ExternalUserID).Scan(&userID, &userStatus)
	if err != nil {
		return AcceptedMessage{}, fmt.Errorf("upsert inbound user: %w", err)
	}
	if userStatus != "ACTIVE" {
		return AcceptedMessage{}, ErrUserDisabled
	}

	var conversationID string
	err = tx.QueryRow(ctx, `
		INSERT INTO conversations (tenant_id, user_id, channel)
		VALUES ($1::uuid, $2::uuid, 'WECOM')
		ON CONFLICT (tenant_id, user_id, channel)
		DO UPDATE SET updated_at = now(), status = 'ACTIVE'
		RETURNING id::text
	`, input.TenantID, userID).Scan(&conversationID)
	if err != nil {
		return AcceptedMessage{}, fmt.Errorf("upsert conversation: %w", err)
	}

	requestID := uuid.NewString()
	inputMessageID := uuid.NewString()
	_, err = tx.Exec(ctx, `
		INSERT INTO messages (
			id, tenant_id, conversation_id, user_id, request_id, role, content
		) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, 'USER', $6)
	`, inputMessageID, input.TenantID, conversationID, userID, requestID, input.Content)
	if err != nil {
		return AcceptedMessage{}, fmt.Errorf("insert inbound message: %w", err)
	}

	var inboundID string
	err = tx.QueryRow(ctx, `
		INSERT INTO inbound_messages (
			tenant_id, integration_id, external_message_id, external_user_id,
			request_id, input_message_id, user_id, conversation_id, received_at
		) VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7::uuid, $8::uuid, $9)
		ON CONFLICT (tenant_id, integration_id, external_message_id) DO NOTHING
		RETURNING id::text
	`, input.TenantID, input.IntegrationID, input.ExternalMessageID, input.ExternalUserID,
		requestID, inputMessageID, userID, conversationID, input.ReceivedAt).Scan(&inboundID)
	if errors.Is(err, pgx.ErrNoRows) {
		_ = tx.Rollback(ctx)
		existing, found, lookupErr := s.findInbound(ctx, input.TenantID, input.IntegrationID, input.ExternalMessageID)
		if lookupErr != nil {
			return AcceptedMessage{}, lookupErr
		}
		if !found {
			return AcceptedMessage{}, errors.New("duplicate inbound message disappeared")
		}
		existing.Duplicate = true
		return existing, nil
	}
	if err != nil {
		return AcceptedMessage{}, fmt.Errorf("insert inbound record: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return AcceptedMessage{}, fmt.Errorf("commit inbound transaction: %w", err)
	}
	return AcceptedMessage{
		RequestID:      requestID,
		InputMessageID: inputMessageID,
		UserID:         userID,
		ConversationID: conversationID,
	}, nil
}

func (s *Store) MarkInboundQueued(ctx context.Context, tenantID, requestID string) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE inbound_messages
		SET queued_at = COALESCE(queued_at, now())
		WHERE tenant_id = $1::uuid AND request_id = $2
	`, tenantID, requestID)
	if err != nil {
		return fmt.Errorf("mark inbound queued: %w", err)
	}
	if command.RowsAffected() != 1 {
		return fmt.Errorf("mark inbound queued: request %s not found", requestID)
	}
	return nil
}

func (s *Store) LoadQuestion(ctx context.Context, tenantID, inputMessageID string) (Question, error) {
	var question Question
	err := s.pool.QueryRow(ctx, `
		SELECT
			m.tenant_id::text,
			m.request_id,
			m.id::text,
			m.user_id::text,
			m.conversation_id::text,
			m.content,
			c.channel
		FROM messages m
		JOIN conversations c ON c.id = m.conversation_id AND c.tenant_id = m.tenant_id
		WHERE m.tenant_id = $1::uuid AND m.id = $2::uuid AND m.role = 'USER'
	`, tenantID, inputMessageID).Scan(
		&question.TenantID,
		&question.RequestID,
		&question.InputMessageID,
		&question.UserID,
		&question.ConversationID,
		&question.Content,
		&question.Channel,
	)
	if err != nil {
		return Question{}, fmt.Errorf("load question: %w", err)
	}
	return question, nil
}

func (s *Store) LoadOutboundAnswer(
	ctx context.Context,
	tenantID string,
	messageID string,
	recipientUserID string,
) (OutboundAnswer, error) {
	var answer OutboundAnswer
	err := s.pool.QueryRow(ctx, `
		SELECT m.id::text, m.user_id::text, u.wecom_user_id, m.content
		FROM messages m
		JOIN users u ON u.id = m.user_id AND u.tenant_id = m.tenant_id
		WHERE m.tenant_id = $1::uuid AND m.id = $2::uuid
		  AND m.user_id = $3::uuid AND m.role = 'ASSISTANT'
		  AND u.status = 'ACTIVE'
	`, tenantID, messageID, recipientUserID).Scan(
		&answer.MessageID,
		&answer.InternalUserID,
		&answer.ExternalUserID,
		&answer.Content,
	)
	if err != nil {
		return OutboundAnswer{}, fmt.Errorf("load outbound answer: %w", err)
	}
	rows, err := s.pool.Query(ctx, `
		SELECT citation_index, title_snapshot, locator_type, locator_value
		FROM citations
		WHERE tenant_id = $1::uuid AND message_id = $2::uuid
		ORDER BY citation_index
	`, tenantID, messageID)
	if err != nil {
		return OutboundAnswer{}, fmt.Errorf("load outbound citations: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var citation OutboundCitation
		if err := rows.Scan(
			&citation.Index,
			&citation.Title,
			&citation.LocatorType,
			&citation.LocatorValue,
		); err != nil {
			return OutboundAnswer{}, fmt.Errorf("scan outbound citation: %w", err)
		}
		answer.Citations = append(answer.Citations, citation)
	}
	if err := rows.Err(); err != nil {
		return OutboundAnswer{}, fmt.Errorf("iterate outbound citations: %w", err)
	}
	return answer, nil
}

func (s *Store) ClaimOutboundDelivery(
	ctx context.Context,
	tenantID string,
	eventID string,
	requestID string,
	messageID string,
	recipientUserID string,
	channel string,
) (bool, error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return false, fmt.Errorf("begin outbound claim: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var currentStatus string
	err = tx.QueryRow(ctx, `
		INSERT INTO outbound_deliveries (
			tenant_id, event_id, request_id, message_id, recipient_user_id, channel
		) VALUES ($1::uuid, $2::uuid, $3, $4::uuid, $5::uuid, $6)
		ON CONFLICT (tenant_id, event_id)
		DO UPDATE SET updated_at = now()
		RETURNING status
	`, tenantID, eventID, requestID, messageID, recipientUserID, channel).Scan(&currentStatus)
	if err != nil {
		return false, fmt.Errorf("upsert outbound delivery: %w", err)
	}
	if currentStatus == "SENT" {
		if err := tx.Commit(ctx); err != nil {
			return false, fmt.Errorf("commit completed outbound claim: %w", err)
		}
		return false, nil
	}
	command, err := tx.Exec(ctx, `
		UPDATE outbound_deliveries
		SET status = 'SENDING', attempt = attempt + 1,
		    last_error = NULL, updated_at = now()
		WHERE tenant_id = $1::uuid AND event_id = $2::uuid
	`, tenantID, eventID)
	if err != nil {
		return false, fmt.Errorf("claim outbound delivery: %w", err)
	}
	if command.RowsAffected() != 1 {
		return false, errors.New("claim outbound delivery affected no rows")
	}
	if err := tx.Commit(ctx); err != nil {
		return false, fmt.Errorf("commit outbound claim: %w", err)
	}
	return true, nil
}

func (s *Store) MarkOutboundSent(
	ctx context.Context,
	tenantID string,
	eventID string,
	externalMessageID string,
) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE outbound_deliveries
		SET status = 'SENT', external_message_id = NULLIF($3, ''),
		    sent_at = now(), updated_at = now()
		WHERE tenant_id = $1::uuid AND event_id = $2::uuid
	`, tenantID, eventID, externalMessageID)
	if err != nil {
		return fmt.Errorf("mark outbound sent: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errors.New("mark outbound sent affected no rows")
	}
	return nil
}

func (s *Store) MarkOutboundFailed(
	ctx context.Context,
	tenantID string,
	eventID string,
	cause error,
) error {
	message := cause.Error()
	if len(message) > 1000 {
		message = message[:1000]
	}
	_, err := s.pool.Exec(ctx, `
		UPDATE outbound_deliveries
		SET status = 'RETRYABLE_FAILED', last_error = $3, updated_at = now()
		WHERE tenant_id = $1::uuid AND event_id = $2::uuid
	`, tenantID, eventID, message)
	if err != nil {
		return fmt.Errorf("mark outbound failed: %w", err)
	}
	return nil
}

func (s *Store) findInbound(
	ctx context.Context,
	tenantID string,
	integrationID string,
	externalMessageID string,
) (AcceptedMessage, bool, error) {
	var result AcceptedMessage
	err := s.pool.QueryRow(ctx, `
		SELECT
			request_id,
			input_message_id::text,
			user_id::text,
			conversation_id::text,
			queued_at IS NOT NULL
		FROM inbound_messages
		WHERE tenant_id = $1::uuid AND integration_id = $2 AND external_message_id = $3
	`, tenantID, integrationID, externalMessageID).Scan(
		&result.RequestID,
		&result.InputMessageID,
		&result.UserID,
		&result.ConversationID,
		&result.Queued,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return AcceptedMessage{}, false, nil
	}
	if err != nil {
		return AcceptedMessage{}, false, fmt.Errorf("find inbound message: %w", err)
	}
	return result, true, nil
}
