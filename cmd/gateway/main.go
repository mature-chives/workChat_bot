package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"workchat_bot/internal/config"
	"workchat_bot/internal/httpapi"
	"workchat_bot/internal/queue"
	"workchat_bot/internal/store"
	"workchat_bot/internal/wecom"
)

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

	var weComCrypto *wecom.Crypto
	if cfg.WeComEnabled() {
		weComCrypto, err = wecom.NewCrypto(
			cfg.WeComCallbackToken,
			cfg.WeComEncodingAESKey,
			cfg.WeComCorpID,
		)
		if err != nil {
			logger.Error("initialize wecom crypto failed", "error", err)
			os.Exit(1)
		}
	} else {
		logger.Warn("wecom callback is disabled because credentials are incomplete")
	}

	router := httpapi.NewRouter(httpapi.Dependencies{
		Config:   cfg,
		Store:    database,
		Database: database,
		Queue:    redisQueue,
		Redis:    redisQueue,
		WeCom:    weComCrypto,
		Logger:   logger,
	})
	server := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		logger.Info("gateway listening", "address", cfg.HTTPAddr)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("gateway server stopped unexpectedly", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		logger.Error("gateway shutdown failed", "error", err)
	}
}
