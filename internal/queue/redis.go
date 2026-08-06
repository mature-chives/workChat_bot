package queue

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

type Redis struct {
	client *redis.Client
	prefix string
}

func NewRedis(addr, password, prefix string) *Redis {
	return &Redis{
		client: redis.NewClient(&redis.Options{Addr: addr, Password: password}),
		prefix: strings.TrimSuffix(prefix, ":"),
	}
}

func (r *Redis) Close() error {
	return r.client.Close()
}

func (r *Redis) Ping(ctx context.Context) error {
	return r.client.Ping(ctx).Err()
}

func (r *Redis) QARequestedStream() string {
	return r.prefix + ":qa:requested"
}

func (r *Redis) QACompletedStream() string {
	return r.prefix + ":qa:completed"
}

func (r *Redis) Publish(ctx context.Context, stream string, event any) (string, error) {
	payload, err := json.Marshal(event)
	if err != nil {
		return "", fmt.Errorf("marshal event: %w", err)
	}
	return r.client.XAdd(ctx, &redis.XAddArgs{
		Stream: stream,
		Values: map[string]any{
			"payload": payload,
		},
	}).Result()
}

func (r *Redis) EnsureGroup(ctx context.Context, stream, group string) error {
	err := r.client.XGroupCreateMkStream(ctx, stream, group, "0").Err()
	if err != nil && !strings.Contains(err.Error(), "BUSYGROUP") {
		return err
	}
	return nil
}

func (r *Redis) ReadGroup(
	ctx context.Context,
	stream string,
	group string,
	consumer string,
	count int64,
) ([]redis.XMessage, error) {
	streams, err := r.client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    group,
		Consumer: consumer,
		Streams:  []string{stream, ">"},
		Count:    count,
		Block:    5 * time.Second,
	}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if len(streams) == 0 {
		return nil, nil
	}
	return streams[0].Messages, nil
}

func (r *Redis) ClaimStale(
	ctx context.Context,
	stream string,
	group string,
	consumer string,
	minIdle time.Duration,
	count int64,
) ([]redis.XMessage, error) {
	messages, _, err := r.client.XAutoClaim(ctx, &redis.XAutoClaimArgs{
		Stream:   stream,
		Group:    group,
		Consumer: consumer,
		MinIdle:  minIdle,
		Start:    "0-0",
		Count:    count,
	}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	return messages, err
}

func (r *Redis) Ack(ctx context.Context, stream, group string, ids ...string) error {
	return r.client.XAck(ctx, stream, group, ids...).Err()
}

func DecodeMessage[T any](message redis.XMessage) (Envelope[T], error) {
	value, ok := message.Values["payload"]
	if !ok {
		return Envelope[T]{}, fmt.Errorf("stream message %s has no payload", message.ID)
	}
	var raw []byte
	switch typed := value.(type) {
	case string:
		raw = []byte(typed)
	case []byte:
		raw = typed
	default:
		return Envelope[T]{}, fmt.Errorf("stream message %s payload has type %T", message.ID, value)
	}
	var event Envelope[T]
	if err := json.Unmarshal(raw, &event); err != nil {
		return Envelope[T]{}, fmt.Errorf("decode stream message %s: %w", message.ID, err)
	}
	return event, nil
}
