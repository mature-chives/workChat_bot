package wecom

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

const apiResponseLimit = 1 << 20

type APIClient struct {
	baseURL   string
	corpID    string
	secret    string
	agentID   int64
	http      *http.Client
	mu        sync.Mutex
	token     string
	expiresAt time.Time
}

type apiResponse struct {
	ErrorCode    int    `json:"errcode"`
	ErrorMessage string `json:"errmsg"`
	AccessToken  string `json:"access_token"`
	ExpiresIn    int    `json:"expires_in"`
	MessageID    string `json:"msgid"`
}

func NewAPIClient(baseURL, corpID, secret, rawAgentID string) (*APIClient, error) {
	agentID, err := strconv.ParseInt(rawAgentID, 10, 64)
	if err != nil || agentID <= 0 {
		return nil, errors.New("wecom agent ID must be a positive integer")
	}
	if strings.TrimSpace(baseURL) == "" || corpID == "" || secret == "" {
		return nil, errors.New("wecom API URL, corp ID and secret are required")
	}
	return &APIClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		corpID:  corpID,
		secret:  secret,
		agentID: agentID,
		http:    &http.Client{Timeout: 10 * time.Second},
	}, nil
}

func (c *APIClient) SendText(ctx context.Context, recipient, content string) (string, error) {
	if recipient == "" || strings.TrimSpace(content) == "" {
		return "", errors.New("wecom recipient and content are required")
	}
	token, err := c.accessToken(ctx, false)
	if err != nil {
		return "", err
	}
	messageID, code, err := c.sendText(ctx, token, recipient, content)
	if err == nil {
		return messageID, nil
	}
	if code != 40014 && code != 42001 {
		return "", err
	}
	token, tokenErr := c.accessToken(ctx, true)
	if tokenErr != nil {
		return "", tokenErr
	}
	messageID, _, err = c.sendText(ctx, token, recipient, content)
	return messageID, err
}

func (c *APIClient) accessToken(ctx context.Context, forceRefresh bool) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !forceRefresh && c.token != "" && time.Now().Before(c.expiresAt) {
		return c.token, nil
	}
	query := url.Values{"corpid": {c.corpID}, "corpsecret": {c.secret}}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodGet, c.baseURL+"/cgi-bin/gettoken?"+query.Encode(), nil,
	)
	if err != nil {
		return "", fmt.Errorf("create wecom token request: %w", err)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return "", fmt.Errorf("request wecom access token: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("request wecom access token: HTTP %d", response.StatusCode)
	}
	var result apiResponse
	if err := decodeAPIResponse(response.Body, &result); err != nil {
		return "", fmt.Errorf("decode wecom access token: %w", err)
	}
	if result.ErrorCode != 0 || result.AccessToken == "" {
		return "", fmt.Errorf(
			"request wecom access token: code=%d message=%s",
			result.ErrorCode,
			result.ErrorMessage,
		)
	}
	ttl := time.Duration(result.ExpiresIn) * time.Second
	if ttl <= 10*time.Minute {
		ttl /= 2
	} else {
		ttl -= 5 * time.Minute
	}
	c.token = result.AccessToken
	c.expiresAt = time.Now().Add(ttl)
	return c.token, nil
}

func (c *APIClient) sendText(
	ctx context.Context,
	token string,
	recipient string,
	content string,
) (string, int, error) {
	payload := struct {
		ToUser                 string `json:"touser"`
		MessageType            string `json:"msgtype"`
		AgentID                int64  `json:"agentid"`
		Text                   any    `json:"text"`
		EnableDuplicateCheck   int    `json:"enable_duplicate_check"`
		DuplicateCheckInterval int    `json:"duplicate_check_interval"`
	}{
		ToUser:      recipient,
		MessageType: "text",
		AgentID:     c.agentID,
		Text: struct {
			Content string `json:"content"`
		}{Content: content},
		EnableDuplicateCheck:   1,
		DuplicateCheckInterval: 1800,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return "", 0, fmt.Errorf("encode wecom message: %w", err)
	}
	endpoint := c.baseURL + "/cgi-bin/message/send?access_token=" + url.QueryEscape(token)
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, endpoint, strings.NewReader(string(body)),
	)
	if err != nil {
		return "", 0, fmt.Errorf("create wecom message request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := c.http.Do(request)
	if err != nil {
		return "", 0, fmt.Errorf("send wecom message: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", 0, fmt.Errorf("send wecom message: HTTP %d", response.StatusCode)
	}
	var result apiResponse
	if err := decodeAPIResponse(response.Body, &result); err != nil {
		return "", 0, fmt.Errorf("decode wecom message response: %w", err)
	}
	if result.ErrorCode != 0 {
		return "", result.ErrorCode, fmt.Errorf(
			"send wecom message: code=%d message=%s",
			result.ErrorCode,
			result.ErrorMessage,
		)
	}
	return result.MessageID, 0, nil
}

func decodeAPIResponse(reader io.Reader, destination any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, apiResponseLimit))
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	return nil
}
