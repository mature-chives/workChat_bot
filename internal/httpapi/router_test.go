package httpapi

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"workchat_bot/internal/config"
	"workchat_bot/internal/queue"
	"workchat_bot/internal/store"
	"workchat_bot/internal/wecom"
)

type healthy struct{}

func (healthy) Ping(context.Context) error { return nil }

type callbackStore struct {
	input      store.InboundText
	markCalled bool
}

func (s *callbackStore) AcceptInboundText(
	ctx context.Context, input store.InboundText,
) (store.AcceptedMessage, error) {
	s.input = input
	return store.AcceptedMessage{
		RequestID:      "request-1",
		InputMessageID: "00000000-0000-0000-0000-000000000004",
		UserID:         "00000000-0000-0000-0000-000000000002",
		ConversationID: "00000000-0000-0000-0000-000000000003",
	}, nil
}

func (s *callbackStore) MarkInboundQueued(
	ctx context.Context, tenantID string, requestID string,
) error {
	s.markCalled = true
	return nil
}

type callbackQueue struct {
	event any
}

func (q *callbackQueue) Publish(ctx context.Context, stream string, event any) (string, error) {
	q.event = event
	return "1-0", nil
}

func (q *callbackQueue) QARequestedStream() string { return "qa:requested" }

func TestReceiveWeComTextPersistsAndPublishes(t *testing.T) {
	cryptoService, key := callbackCrypto(t)
	database := &callbackStore{}
	events := &callbackQueue{}
	router := NewRouter(Dependencies{
		Config: config.Config{
			TenantID:     "00000000-0000-0000-0000-000000000001",
			WeComAgentID: "1000002",
		},
		Store:    database,
		Database: healthy{},
		Queue:    events,
		Redis:    healthy{},
		WeCom:    cryptoService,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	plain := []byte(`<xml><FromUserName>zhangsan</FromUserName><CreateTime>1700000000</CreateTime><MsgType>text</MsgType><Content>客户开户需要哪些资料</Content><MsgId>message-1</MsgId><AgentID>1000002</AgentID></xml>`)
	ciphertext := encryptCallback(t, key, plain, "corp-id")
	timestamp := fmt.Sprint(time.Now().Unix())
	query := url.Values{
		"timestamp":     {timestamp},
		"nonce":         {"nonce-1"},
		"msg_signature": {cryptoService.Signature(timestamp, "nonce-1", ciphertext)},
	}
	body := `<xml><Encrypt><![CDATA[` + ciphertext + `]]></Encrypt></xml>`
	request := httptest.NewRequest(
		http.MethodPost, "/callbacks/wecom?"+query.Encode(), strings.NewReader(body),
	)
	recorder := httptest.NewRecorder()

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK || recorder.Body.String() != "success" {
		t.Fatalf("callback response = %d %q", recorder.Code, recorder.Body.String())
	}
	if database.input.ExternalUserID != "zhangsan" ||
		database.input.Content != "客户开户需要哪些资料" || !database.markCalled {
		t.Fatalf("unexpected stored callback: %+v", database.input)
	}
	event, ok := events.event.(queue.Envelope[queue.QARequestedData])
	if !ok {
		t.Fatalf("published event has type %T", events.event)
	}
	if event.EventType != queue.EventQARequested || event.RequestID != "request-1" ||
		event.Data.ActorUserID != "00000000-0000-0000-0000-000000000002" {
		t.Fatalf("unexpected event: %+v", event)
	}
}

func TestReceiveWeComRejectsInvalidSignature(t *testing.T) {
	cryptoService, key := callbackCrypto(t)
	router := NewRouter(Dependencies{
		Config:   config.Config{WeComAgentID: "1000002"},
		Store:    &callbackStore{},
		Database: healthy{},
		Queue:    &callbackQueue{},
		Redis:    healthy{},
		WeCom:    cryptoService,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	ciphertext := encryptCallback(t, key, []byte("<xml></xml>"), "corp-id")
	target := fmt.Sprintf(
		"/callbacks/wecom?timestamp=%d&nonce=n&msg_signature=bad", time.Now().Unix(),
	)
	body := `<xml><Encrypt><![CDATA[` + ciphertext + `]]></Encrypt></xml>`
	request := httptest.NewRequest(http.MethodPost, target, strings.NewReader(body))
	recorder := httptest.NewRecorder()

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("callback status = %d, want %d", recorder.Code, http.StatusForbidden)
	}
}

func callbackCrypto(t *testing.T) (*wecom.Crypto, []byte) {
	t.Helper()
	key := []byte("0123456789abcdef0123456789abcdef")
	encodingKey := strings.TrimSuffix(base64.StdEncoding.EncodeToString(key), "=")
	service, err := wecom.NewCrypto("callback-token", encodingKey, "corp-id")
	if err != nil {
		t.Fatalf("NewCrypto() error = %v", err)
	}
	return service, key
}

func encryptCallback(t *testing.T, key, message []byte, receiveID string) string {
	t.Helper()
	payload := make([]byte, 20+len(message)+len(receiveID))
	copy(payload[:16], []byte("0123456789abcdef"))
	binary.BigEndian.PutUint32(payload[16:20], uint32(len(message)))
	copy(payload[20:], message)
	copy(payload[20+len(message):], receiveID)
	padding := 32 - len(payload)%32
	for range padding {
		payload = append(payload, byte(padding))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("aes.NewCipher() error = %v", err)
	}
	encrypted := make([]byte, len(payload))
	cipher.NewCBCEncrypter(block, key[:aes.BlockSize]).CryptBlocks(encrypted, payload)
	return base64.StdEncoding.EncodeToString(encrypted)
}
