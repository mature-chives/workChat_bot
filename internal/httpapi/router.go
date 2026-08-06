package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"workchat_bot/internal/config"
	"workchat_bot/internal/queue"
	"workchat_bot/internal/store"
	"workchat_bot/internal/wecom"
)

const callbackBodyLimit = 1 << 20

type HealthChecker interface {
	Ping(context.Context) error
}

type EventPublisher interface {
	Publish(context.Context, string, any) (string, error)
	QARequestedStream() string
}

type MessageStore interface {
	AcceptInboundText(context.Context, store.InboundText) (store.AcceptedMessage, error)
	MarkInboundQueued(context.Context, string, string) error
}

type Dependencies struct {
	Config   config.Config
	Store    MessageStore
	Database HealthChecker
	Queue    EventPublisher
	Redis    HealthChecker
	WeCom    *wecom.Crypto
	Logger   *slog.Logger
}

func NewRouter(deps Dependencies) *gin.Engine {
	if deps.Config.Environment == "production" {
		gin.SetMode(gin.ReleaseMode)
	}
	router := gin.New()
	router.Use(gin.Recovery())

	router.GET("/health/live", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "UP"})
	})
	router.GET("/health/ready", readyHandler(deps))
	router.GET("/callbacks/wecom", verifyWeComHandler(deps))
	router.POST("/callbacks/wecom", receiveWeComHandler(deps))
	return router
}

func readyHandler(deps Dependencies) gin.HandlerFunc {
	return func(c *gin.Context) {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 750*time.Millisecond)
		defer cancel()
		components := gin.H{"database": "UP", "redis": "UP"}
		status := "UP"
		if err := deps.Database.Ping(ctx); err != nil {
			components["database"] = "DOWN"
			status = "DOWN"
		}
		if err := deps.Redis.Ping(ctx); err != nil {
			components["redis"] = "DOWN"
			status = "DOWN"
		}
		code := http.StatusOK
		if status == "DOWN" {
			code = http.StatusServiceUnavailable
		}
		c.JSON(code, gin.H{"status": status, "components": components})
	}
}

func verifyWeComHandler(deps Dependencies) gin.HandlerFunc {
	return func(c *gin.Context) {
		if deps.WeCom == nil {
			c.Status(http.StatusServiceUnavailable)
			return
		}
		signature := c.Query("msg_signature")
		timestamp := c.Query("timestamp")
		nonce := c.Query("nonce")
		echo := c.Query("echostr")
		if !validCallbackTimestamp(timestamp, time.Now()) ||
			!deps.WeCom.VerifySignature(signature, timestamp, nonce, echo) {
			c.Status(http.StatusForbidden)
			return
		}
		plain, err := deps.WeCom.Decrypt(echo)
		if err != nil {
			deps.Logger.Warn("wecom URL verification decrypt failed", "error", err)
			c.Status(http.StatusBadRequest)
			return
		}
		c.Data(http.StatusOK, "text/plain; charset=utf-8", plain)
	}
}

func receiveWeComHandler(deps Dependencies) gin.HandlerFunc {
	return func(c *gin.Context) {
		if deps.WeCom == nil {
			c.Status(http.StatusServiceUnavailable)
			return
		}
		timestamp := c.Query("timestamp")
		nonce := c.Query("nonce")
		if !validCallbackTimestamp(timestamp, time.Now()) {
			c.Status(http.StatusBadRequest)
			return
		}

		body, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, callbackBodyLimit))
		if err != nil {
			c.Status(http.StatusRequestEntityTooLarge)
			return
		}
		var envelope wecom.EncryptedEnvelope
		if err := xml.Unmarshal(body, &envelope); err != nil || envelope.Encrypt == "" {
			c.Status(http.StatusBadRequest)
			return
		}
		if !deps.WeCom.VerifySignature(c.Query("msg_signature"), timestamp, nonce, envelope.Encrypt) {
			c.Status(http.StatusForbidden)
			return
		}
		plain, err := deps.WeCom.Decrypt(envelope.Encrypt)
		if err != nil {
			deps.Logger.Warn("wecom callback decrypt failed", "error", err)
			c.Status(http.StatusBadRequest)
			return
		}
		var message wecom.Message
		if err := xml.Unmarshal(plain, &message); err != nil {
			c.Status(http.StatusBadRequest)
			return
		}
		if message.AgentID != "" && message.AgentID != deps.Config.WeComAgentID {
			c.Status(http.StatusBadRequest)
			return
		}
		if message.MsgType != "text" {
			writeSuccess(c)
			return
		}

		content := strings.TrimSpace(message.Content)
		if content == "" || utf8.RuneCountInString(content) > 4000 ||
			message.FromUserName == "" || message.MsgID == "" {
			c.Status(http.StatusBadRequest)
			return
		}
		receivedAt := time.Unix(message.CreateTime, 0).UTC()
		if message.CreateTime <= 0 {
			receivedAt = time.Now().UTC()
		}
		accepted, err := deps.Store.AcceptInboundText(c.Request.Context(), store.InboundText{
			TenantID:          deps.Config.TenantID,
			IntegrationID:     deps.Config.WeComAgentID,
			ExternalMessageID: message.MsgID,
			ExternalUserID:    message.FromUserName,
			Content:           content,
			ReceivedAt:        receivedAt,
		})
		if errors.Is(err, store.ErrUserDisabled) {
			writeSuccess(c)
			return
		}
		if err != nil {
			deps.Logger.Error("persist wecom message failed", "error", err)
			c.Status(http.StatusServiceUnavailable)
			return
		}
		if accepted.Queued {
			writeSuccess(c)
			return
		}

		event := queue.Envelope[queue.QARequestedData]{
			EventID:       uuid.NewString(),
			EventType:     queue.EventQARequested,
			SchemaVersion: "1.0",
			OccurredAt:    time.Now().UTC(),
			TenantID:      deps.Config.TenantID,
			RequestID:     accepted.RequestID,
			TraceID:       newTraceID(),
			Attempt:       1,
			Data: queue.QARequestedData{
				Channel:           "WECOM",
				InputMessageID:    accepted.InputMessageID,
				ActorUserID:       accepted.UserID,
				ConversationID:    accepted.ConversationID,
				ExternalMessageID: message.MsgID,
			},
		}
		if _, err := deps.Queue.Publish(c.Request.Context(), deps.Queue.QARequestedStream(), event); err != nil {
			deps.Logger.Error("publish qa request failed", "request_id", accepted.RequestID, "error", err)
			c.Status(http.StatusServiceUnavailable)
			return
		}
		if err := deps.Store.MarkInboundQueued(c.Request.Context(), deps.Config.TenantID, accepted.RequestID); err != nil {
			deps.Logger.Error("mark qa request queued failed", "request_id", accepted.RequestID, "error", err)
			c.Status(http.StatusServiceUnavailable)
			return
		}
		writeSuccess(c)
	}
}

func validCallbackTimestamp(raw string, now time.Time) bool {
	timestamp, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return false
	}
	delta := now.Sub(time.Unix(timestamp, 0))
	return delta >= -5*time.Minute && delta <= 5*time.Minute
}

func writeSuccess(c *gin.Context) {
	c.Data(http.StatusOK, "text/plain; charset=utf-8", []byte("success"))
}

func newTraceID() string {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	return hex.EncodeToString(buffer)
}
