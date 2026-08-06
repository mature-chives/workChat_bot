package wecom

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func jsonResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func TestAPIClientCachesTokenAndSendsText(t *testing.T) {
	var tokenCalls atomic.Int32
	var sendCalls atomic.Int32
	transport := roundTripFunc(func(request *http.Request) (*http.Response, error) {
		switch request.URL.Path {
		case "/cgi-bin/gettoken":
			tokenCalls.Add(1)
			if request.URL.Query().Get("corpid") != "corp" ||
				request.URL.Query().Get("corpsecret") != "secret" {
				t.Fatal("token request has invalid credentials")
			}
			return jsonResponse(`{"errcode":0,"access_token":"token-1","expires_in":7200}`), nil
		case "/cgi-bin/message/send":
			sendCalls.Add(1)
			if request.URL.Query().Get("access_token") != "token-1" {
				t.Fatal("send request has invalid token")
			}
			var body struct {
				ToUser  string `json:"touser"`
				AgentID int64  `json:"agentid"`
				Text    struct {
					Content string `json:"content"`
				} `json:"text"`
			}
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
				t.Fatalf("decode message body: %v", err)
			}
			if body.ToUser != "zhangsan" || body.AgentID != 1000002 || body.Text.Content != "回答" {
				t.Fatalf("unexpected message body: %+v", body)
			}
			return jsonResponse(`{"errcode":0,"errmsg":"ok","msgid":"message-1"}`), nil
		default:
			return &http.Response{
				StatusCode: http.StatusNotFound,
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader("not found")),
			}, nil
		}
	})

	client, err := NewAPIClient("https://wecom.invalid", "corp", "secret", "1000002")
	if err != nil {
		t.Fatalf("NewAPIClient() error = %v", err)
	}
	client.http.Transport = transport
	for range 2 {
		messageID, err := client.SendText(context.Background(), "zhangsan", "回答")
		if err != nil {
			t.Fatalf("SendText() error = %v", err)
		}
		if messageID != "message-1" {
			t.Fatalf("SendText() message ID = %q", messageID)
		}
	}
	if got := tokenCalls.Load(); got != 1 {
		t.Fatalf("token endpoint called %d times, want 1", got)
	}
	if got := sendCalls.Load(); got != 2 {
		t.Fatalf("send endpoint called %d times, want 2", got)
	}
}

func TestAPIClientReturnsWeComError(t *testing.T) {
	transport := roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path == "/cgi-bin/gettoken" {
			return jsonResponse(`{"errcode":0,"access_token":"token-1","expires_in":7200}`), nil
		}
		return jsonResponse(`{"errcode":81013,"errmsg":"user & party & tag all invalid"}`), nil
	})
	client, err := NewAPIClient("https://wecom.invalid", "corp", "secret", "1000002")
	if err != nil {
		t.Fatalf("NewAPIClient() error = %v", err)
	}
	client.http.Transport = transport

	if _, err := client.SendText(context.Background(), "missing", "回答"); err == nil {
		t.Fatal("SendText() returned nil error")
	}
}
