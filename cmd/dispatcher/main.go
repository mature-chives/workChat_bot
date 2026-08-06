package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"

	"workchat_bot/internal/agentclient"
	"workchat_bot/internal/config"
	"workchat_bot/internal/queue"
	"workchat_bot/internal/store"
)

const (
	consumerGroup = "agent-dispatcher"
	batchSize     = 8
)

type dispatcher struct {
	store    *store.Store
	queue    *queue.Redis
	agent    *agentclient.Client
	consumer string
	logger   *slog.Logger
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := config.Load()
	if err != nil {
		logger.Error("load config failed", "error", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	database, err := store.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Error("open database failed", "error", err)
		os.Exit(1)
	}
	defer database.Close()

	redisQueue := queue.NewRedis(cfg.RedisAddr, cfg.RedisPassword, cfg.RedisStreamPrefix)
	defer func() { _ = redisQueue.Close() }()
	if err := redisQueue.Ping(ctx); err != nil {
		logger.Error("connect redis failed", "error", err)
		os.Exit(1)
	}
	if err := redisQueue.EnsureGroup(ctx, redisQueue.QARequestedStream(), consumerGroup); err != nil {
		logger.Error("create dispatcher consumer group failed", "error", err)
		os.Exit(1)
	}

	agent, err := agentclient.Dial(cfg.AgentGRPCAddr, 15*time.Second)
	if err != nil {
		logger.Error("create agent client failed", "error", err)
		os.Exit(1)
	}
	defer func() { _ = agent.Close() }()

	hostname, _ := os.Hostname()
	worker := &dispatcher{
		store:    database,
		queue:    redisQueue,
		agent:    agent,
		consumer: hostname + "-" + strconv.Itoa(os.Getpid()),
		logger:   logger,
	}
	logger.Info("dispatcher started", "consumer", worker.consumer)
	if err := worker.run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		logger.Error("dispatcher stopped unexpectedly", "error", err)
		os.Exit(1)
	}
}

func (d *dispatcher) run(ctx context.Context) error {
	stream := d.queue.QARequestedStream()
	for {
		messages, err := d.queue.ClaimStale(
			ctx, stream, consumerGroup, d.consumer, 30*time.Second, batchSize,
		)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return err
			}
			d.logger.Warn("claim stale messages failed", "error", err)
		} else if len(messages) > 0 {
			d.processBatch(ctx, messages)
			continue
		}

		messages, err = d.queue.ReadGroup(ctx, stream, consumerGroup, d.consumer, batchSize)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return err
			}
			d.logger.Warn("read request stream failed", "error", err)
			if !waitForRetry(ctx, time.Second) {
				return ctx.Err()
			}
			continue
		}
		d.processBatch(ctx, messages)
	}
}

func (d *dispatcher) processBatch(ctx context.Context, messages []redis.XMessage) {
	for _, message := range messages {
		if err := d.processOne(ctx, message); err != nil {
			d.logger.Error("process qa request failed", "stream_id", message.ID, "error", err)
		}
	}
}

func (d *dispatcher) processOne(ctx context.Context, message redis.XMessage) error {
	event, err := queue.DecodeMessage[queue.QARequestedData](message)
	if err != nil {
		d.ackPoison(ctx, message.ID, err)
		return nil
	}
	if err := validateRequestedEvent(event); err != nil {
		d.ackPoison(ctx, message.ID, err)
		return nil
	}

	question, err := d.store.LoadQuestion(ctx, event.TenantID, event.Data.InputMessageID)
	if err != nil {
		return err
	}
	if question.RequestID != event.RequestID || question.UserID != event.Data.ActorUserID {
		d.ackPoison(ctx, message.ID, errors.New("event identity does not match stored question"))
		return nil
	}
	answer, err := d.agent.AnswerQuestion(ctx, question, event.TraceID)
	if err != nil {
		return err
	}
	if answer.ConversationID != question.ConversationID || answer.MessageID == "" {
		return errors.New("agent returned an invalid answer identity")
	}

	causationID := event.EventID
	completed := queue.Envelope[queue.QACompletedData]{
		EventID: uuid.NewSHA1(
			uuid.NameSpaceOID,
			[]byte(event.TenantID+":"+event.RequestID+":"+queue.EventQACompleted),
		).String(),
		EventType:        queue.EventQACompleted,
		SchemaVersion:    "1.0",
		OccurredAt:       time.Now().UTC(),
		TenantID:         event.TenantID,
		RequestID:        event.RequestID,
		TraceID:          event.TraceID,
		CausationEventID: &causationID,
		Attempt:          event.Attempt,
		Data: queue.QACompletedData{
			MessageID:       answer.MessageID,
			RecipientUserID: event.Data.ActorUserID,
			Channel:         event.Data.Channel,
		},
	}
	if _, err := d.queue.Publish(ctx, d.queue.QACompletedStream(), completed); err != nil {
		return fmt.Errorf("publish qa completion: %w", err)
	}
	if err := d.queue.Ack(ctx, d.queue.QARequestedStream(), consumerGroup, message.ID); err != nil {
		return fmt.Errorf("ack qa request: %w", err)
	}
	d.logger.Info(
		"qa request completed",
		"request_id", event.RequestID,
		"message_id", answer.MessageID,
		"refused", answer.Refused,
	)
	return nil
}

func (d *dispatcher) ackPoison(ctx context.Context, messageID string, cause error) {
	d.logger.Error("discard invalid qa request", "stream_id", messageID, "error", cause)
	if err := d.queue.Ack(ctx, d.queue.QARequestedStream(), consumerGroup, messageID); err != nil {
		d.logger.Error("ack invalid qa request failed", "stream_id", messageID, "error", err)
	}
}

func validateRequestedEvent(event queue.Envelope[queue.QARequestedData]) error {
	if event.EventType != queue.EventQARequested || event.SchemaVersion != "1.0" {
		return errors.New("unsupported event type or schema version")
	}
	if _, err := uuid.Parse(event.EventID); err != nil {
		return errors.New("invalid event ID")
	}
	if _, err := uuid.Parse(event.TenantID); err != nil {
		return errors.New("invalid tenant ID")
	}
	if event.RequestID == "" || event.TraceID == "" || event.Data.InputMessageID == "" ||
		event.Data.ActorUserID == "" || event.Data.Channel == "" {
		return errors.New("qa request is missing required fields")
	}
	return nil
}

func waitForRetry(ctx context.Context, duration time.Duration) bool {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
