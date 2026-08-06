package queue

import "time"

const (
	EventQARequested = "qa.requested"
	EventQACompleted = "qa.completed"
)

type Envelope[T any] struct {
	EventID          string    `json:"event_id"`
	EventType        string    `json:"event_type"`
	SchemaVersion    string    `json:"schema_version"`
	OccurredAt       time.Time `json:"occurred_at"`
	TenantID         string    `json:"tenant_id"`
	RequestID        string    `json:"request_id"`
	TraceID          string    `json:"trace_id"`
	CausationEventID *string   `json:"causation_event_id,omitempty"`
	Attempt          int       `json:"attempt"`
	Data             T         `json:"data"`
}

type QARequestedData struct {
	Channel           string `json:"channel"`
	InputMessageID    string `json:"input_message_id"`
	ActorUserID       string `json:"actor_user_id"`
	ConversationID    string `json:"conversation_id"`
	ExternalMessageID string `json:"external_message_id"`
}

type QACompletedData struct {
	MessageID       string `json:"message_id"`
	RecipientUserID string `json:"recipient_user_id"`
	Channel         string `json:"channel"`
}
