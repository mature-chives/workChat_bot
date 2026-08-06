package config

import (
	"fmt"
	"os"
	"strings"
)

type Config struct {
	Environment         string
	HTTPAddr            string
	DatabaseURL         string
	RedisAddr           string
	RedisPassword       string
	RedisStreamPrefix   string
	AgentGRPCAddr       string
	TenantID            string
	WeComCorpID         string
	WeComAgentID        string
	WeComCallbackToken  string
	WeComEncodingAESKey string
	WeComCorpSecret     string
	WeComAPIBaseURL     string
}

func Load() (Config, error) {
	cfg := Config{
		Environment:         envOr("APP_ENV", "development"),
		HTTPAddr:            envOr("GATEWAY_HTTP_ADDR", ":8080"),
		DatabaseURL:         os.Getenv("DATABASE_URL"),
		RedisAddr:           envOr("REDIS_ADDR", "127.0.0.1:6379"),
		RedisPassword:       os.Getenv("REDIS_PASSWORD"),
		RedisStreamPrefix:   envOr("REDIS_STREAM_PREFIX", "wxagent:v1"),
		AgentGRPCAddr:       envOr("AGENT_GRPC_ADDR", "127.0.0.1:50051"),
		TenantID:            envOr("TENANT_ID", "00000000-0000-0000-0000-000000000001"),
		WeComCorpID:         os.Getenv("WECOM_CORP_ID"),
		WeComAgentID:        os.Getenv("WECOM_AGENT_ID"),
		WeComCallbackToken:  os.Getenv("WECOM_CALLBACK_TOKEN"),
		WeComEncodingAESKey: os.Getenv("WECOM_ENCODING_AES_KEY"),
		WeComCorpSecret:     os.Getenv("WECOM_CORP_SECRET"),
		WeComAPIBaseURL:     envOr("WECOM_API_BASE_URL", "https://qyapi.weixin.qq.com"),
	}

	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		return Config{}, fmt.Errorf("DATABASE_URL is required")
	}
	return cfg, nil
}

func (c Config) WeComOutboundEnabled() bool {
	return c.WeComCorpID != "" && c.WeComAgentID != "" && c.WeComCorpSecret != ""
}

func (c Config) WeComEnabled() bool {
	return c.WeComCorpID != "" && c.WeComAgentID != "" &&
		c.WeComCallbackToken != "" && c.WeComEncodingAESKey != ""
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}
