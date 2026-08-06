package main

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"

	"workchat_bot/internal/config"
	"workchat_bot/internal/outbound"
	"workchat_bot/internal/queue"
	"workchat_bot/internal/store"
	"workchat_bot/internal/wecom"
)

const (
	consumerGroup = "wecom-outbound"
	batchSize     = 8
)

type worker struct {
	store    *store.Store
	queue    *queue.Redis
	wecom    *wecom.APIClient
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
	if !cfg.WeComOutboundEnabled() {
		logger.Error("wecom outbound credentials are incomplete")
		os.Exit(1)
	}
	api, err := wecom.NewAPIClient(
		cfg.WeComAPIBaseURL, cfg.WeComCorpID, cfg.WeComCorpSecret, cfg.WeComAgentID,
	)
	if err != nil {
		logger.Error("initialize wecom API client failed", "error", err)
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
	if err := redisQueue.EnsureGroup(ctx, redisQueue.QACompletedStream(), consumerGroup); err != nil {
		logger.Error("create outbound consumer group failed", "error", err)
		os.Exit(1)
	}
	hostname, _ := os.Hostname()
	service := &worker{
		store:    database,
		queue:    redisQueue,
		wecom:    api,
		consumer: hostname + "-" + strconv.Itoa(os.Getpid()),
		logger:   logger,
	}
	logger.Info("wecom outbound worker started", "consumer", service.consumer)
	if err := service.run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		logger.Error("wecom outbound worker stopped unexpectedly", "error", err)
		os.Exit(1)
	}
}

func (w *worker) run(ctx context.Context) error {
	stream := w.queue.QACompletedStream()
	for {
		messages, err := w.queue.ClaimStale(
			ctx, stream, consumerGroup, w.consumer, 30*time.Second, batchSize,
		)
		if err != nil && !errors.Is(err, context.Canceled) {
			w.logger.Warn("claim stale completion messages failed", "error", err)
		} else if err != nil {
			return err
		} else if len(messages) > 0 {
			w.processBatch(ctx, messages)
			continue
		}
		messages, err = w.queue.ReadGroup(ctx, stream, consumerGroup, w.consumer, batchSize)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return err
			}
			w.logger.Warn("read completion stream failed", "error", err)
			if !waitForRetry(ctx, time.Second) {
				return ctx.Err()
			}
			continue
		}
		w.processBatch(ctx, messages)
	}
}

func (w *worker) processBatch(ctx context.Context, messages []redis.XMessage) {
	for _, message := range messages {
		if err := w.processOne(ctx, message); err != nil {
			w.logger.Error("send completed answer failed", "stream_id", message.ID, "error", err)
		}
	}
}

func (w *worker) processOne(ctx context.Context, message redis.XMessage) error {
	event, err := queue.DecodeMessage[queue.QACompletedData](message)
	if err != nil {
		w.ackPoison(ctx, message.ID, err)
		return nil
	}
	if err := validateCompletedEvent(event); err != nil {
		w.ackPoison(ctx, message.ID, err)
		return nil
	}
	if event.Data.Channel != "WECOM" {
		return w.queue.Ack(ctx, w.queue.QACompletedStream(), consumerGroup, message.ID)
	}
	claimed, err := w.store.ClaimOutboundDelivery(
		ctx,
		event.TenantID,
		event.EventID,
		event.RequestID,
		event.Data.MessageID,
		event.Data.RecipientUserID,
		event.Data.Channel,
	)
	if err != nil {
		return err
	}
	if !claimed {
		return w.queue.Ack(ctx, w.queue.QACompletedStream(), consumerGroup, message.ID)
	}
	answer, err := w.store.LoadOutboundAnswer(
		ctx, event.TenantID, event.Data.MessageID, event.Data.RecipientUserID,
	)
	if err != nil {
		w.recordFailure(ctx, event, err)
		return err
	}
	externalMessageID, err := w.wecom.SendText(
		ctx, answer.ExternalUserID, outbound.FormatWeComText(answer),
	)
	if err != nil {
		w.recordFailure(ctx, event, err)
		return err
	}
	if err := w.store.MarkOutboundSent(
		ctx, event.TenantID, event.EventID, externalMessageID,
	); err != nil {
		return err
	}
	if err := w.queue.Ack(ctx, w.queue.QACompletedStream(), consumerGroup, message.ID); err != nil {
		return err
	}
	w.logger.Info(
		"wecom answer sent",
		"request_id", event.RequestID,
		"recipient", answer.ExternalUserID,
	)
	return nil
}

func (w *worker) recordFailure(
	ctx context.Context,
	event queue.Envelope[queue.QACompletedData],
	cause error,
) {
	if err := w.store.MarkOutboundFailed(ctx, event.TenantID, event.EventID, cause); err != nil {
		w.logger.Error("record outbound failure failed", "error", err)
	}
}

func (w *worker) ackPoison(ctx context.Context, messageID string, cause error) {
	w.logger.Error("discard invalid completion event", "stream_id", messageID, "error", cause)
	if err := w.queue.Ack(ctx, w.queue.QACompletedStream(), consumerGroup, messageID); err != nil {
		w.logger.Error("ack invalid completion event failed", "stream_id", messageID, "error", err)
	}
}

func validateCompletedEvent(event queue.Envelope[queue.QACompletedData]) error {
	if event.EventType != queue.EventQACompleted || event.SchemaVersion != "1.0" {
		return errors.New("unsupported event type or schema version")
	}
	if _, err := uuid.Parse(event.EventID); err != nil {
		return errors.New("invalid event ID")
	}
	if _, err := uuid.Parse(event.TenantID); err != nil {
		return errors.New("invalid tenant ID")
	}
	if _, err := uuid.Parse(event.Data.MessageID); err != nil {
		return errors.New("invalid message ID")
	}
	if _, err := uuid.Parse(event.Data.RecipientUserID); err != nil {
		return errors.New("invalid recipient user ID")
	}
	if event.RequestID == "" || event.Data.Channel == "" {
		return errors.New("completion event is missing required fields")
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
