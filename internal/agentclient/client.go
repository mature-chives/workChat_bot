package agentclient

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	agentv1 "workchat_bot/gen/go/agent/v1"
	"workchat_bot/internal/store"
)

type Client struct {
	connection *grpc.ClientConn
	service    agentv1.AgentServiceClient
	timeout    time.Duration
}

type Answer struct {
	MessageID      string
	ConversationID string
	Refused        bool
}

func Dial(address string, timeout time.Duration) (*Client, error) {
	connection, err := grpc.NewClient(
		address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, fmt.Errorf("create agent gRPC client: %w", err)
	}
	return &Client{
		connection: connection,
		service:    agentv1.NewAgentServiceClient(connection),
		timeout:    timeout,
	}, nil
}

func (c *Client) Close() error {
	return c.connection.Close()
}

func (c *Client) AnswerQuestion(
	ctx context.Context,
	question store.Question,
	traceID string,
) (Answer, error) {
	requestCtx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	response, err := c.service.AnswerQuestion(requestCtx, &agentv1.AnswerQuestionRequest{
		RequestId:      question.RequestID,
		TenantId:       question.TenantID,
		UserId:         question.UserID,
		ConversationId: question.ConversationID,
		Question:       question.Content,
		TraceId:        traceID,
		Channel:        question.Channel,
	})
	if err != nil {
		return Answer{}, fmt.Errorf("answer question through agent: %w", err)
	}
	return Answer{
		MessageID:      response.GetMessageId(),
		ConversationID: response.GetConversationId(),
		Refused:        response.GetRefused(),
	}, nil
}
