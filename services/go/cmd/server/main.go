// Command server is the Ledgerline Go SUT entrypoint: wiring only.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"strconv"
	"syscall"
	"time"

	"ledgerline/server/internal/api"
	"ledgerline/server/internal/cache"
	"ledgerline/server/internal/config"
	"ledgerline/server/internal/db"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/logx"
	"ledgerline/server/internal/metrics"
)

// serviceVersion comes from the optional SERVICE_VERSION env var (compose
// forwards it, "dev" default; same contract as the .NET side's
// RuntimeStatsWriter/EffectiveConfig).
func serviceVersion() string {
	if v := os.Getenv("SERVICE_VERSION"); v != "" {
		return v
	}
	return "dev"
}

func main() {
	if err := run(); err != nil {
		// Last-resort: the async logger may be down; write to stderr and exit.
		slog.New(slog.NewJSONHandler(os.Stderr, nil)).Error("startup failed", slog.String("detail", err.Error()))
		os.Exit(1)
	}
}

func run() error {
	// Bootstrap logger so a config failure is still emitted as a log line.
	bootstrap := logx.NewBootstrapLogger(os.Stderr)
	cfg, err := config.Load()
	if err != nil {
		bootstrap.Error("config load failed", slog.String("detail", err.Error()))
		return err
	}

	aw := logx.NewAsyncWriter(os.Stdout, cfg.LogQueueLen)
	logger := logx.NewLogger(aw)
	slog.SetDefault(logger)

	// GOMEMLIMIT/GOGC are set by env at process start (Go is not cgroup-aware).
	// Read back the effective values for the config line.
	gomaxprocs := runtime.GOMAXPROCS(0)
	gogc := os.Getenv("GOGC")
	if gogc == "" {
		gogc = "100" // Go default; echoed
	}
	gomemlimit := gomemlimitString()

	// Ensure PDF_DIR exists (created at startup, spec).
	if err := os.MkdirAll(cfg.PDFDir, 0o755); err != nil {
		logger.Error("pdf dir create failed", slog.String("detail", err.Error()))
		return err
	}

	ctx := context.Background()

	// Startup deadline so an unreachable DB fails fast instead of hanging
	// (Npgsql's default connect timeout is 15 s; 30 s covers the full
	// warm-to-max fan-out).
	startupCtx, cancelStartup := context.WithTimeout(ctx, 30*time.Second)
	defer cancelStartup()
	pool, err := db.NewPool(startupCtx, cfg)
	if err != nil {
		logger.Error("db pool init failed", slog.String("detail", err.Error()))
		return err
	}
	if err := db.WarmPool(startupCtx, pool); err != nil {
		logger.Error("db pool warm failed", slog.String("detail", err.Error()))
		pool.Close()
		return err
	}

	rcache := cache.New(cfg)
	if err := rcache.Ping(startupCtx); err != nil {
		logger.Error("redis ping failed", slog.String("detail", err.Error()))
		_ = rcache.Close()
		pool.Close()
		return err
	}

	sampler := metrics.NewSampler(pool, rcache.Client())

	srv := api.NewServer(cfg, pool, rcache, logger, sampler,
		runtime.Version(), serviceVersion(), "sqlc", gogc, gomemlimit)
	handler := httpx.NewRouter(logger, srv.Routes())

	httpServer := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           handler,
		ReadHeaderTimeout: cfg.ReadHeaderTimeout,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
	}

	// The one effective-config line (manifest-captured, spec/logging.md).
	logger.LogAttrs(ctx, slog.LevelInfo, "config",
		slog.String("port", cfg.Port),
		slog.Int("db_pool_max", int(cfg.DBPoolMax)),
		slog.Int("db_pool_min", int(cfg.DBPoolMin)),
		slog.Int("db_conn_lifetime_seconds", int(cfg.DBConnLifetime.Seconds())),
		slog.String("redis_addr", cfg.RedisAddr),
		slog.Int("redis_pool_max", cfg.RedisPoolMax),
		slog.Int("cache_ttl_seconds", cfg.CacheTTLSeconds),
		slog.Int("log_queue_len", cfg.LogQueueLen),
		slog.Int64("body_max_bytes", cfg.BodyMaxBytes),
		slog.String("pdf_dir", cfg.PDFDir),
		slog.Int("gomaxprocs", gomaxprocs),
		slog.Int("processor_count", runtime.NumCPU()),
		slog.String("gogc", gogc),
		slog.String("gomemlimit", gomemlimit),
		slog.String("data_layer", "sqlc"),
		slog.String("runtime_version", runtime.Version()),
		slog.String("service_version", serviceVersion()),
		slog.Float64("read_header_timeout_seconds", cfg.ReadHeaderTimeout.Seconds()),
		slog.Float64("read_timeout_seconds", cfg.ReadTimeout.Seconds()),
		slog.Float64("write_timeout_seconds", cfg.WriteTimeout.Seconds()),
		slog.Float64("idle_timeout_seconds", cfg.IdleTimeout.Seconds()),
	)

	// Signal handling BEFORE serving, so a SIGTERM in the startup window still
	// drains gracefully instead of killing the process.
	sigCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	// Serve; block on signal (or a fatal serve error).
	serveErr := make(chan error, 1)
	go func() {
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			serveErr <- err
		}
	}()

	select {
	case err := <-serveErr:
		logger.Error("serve failed", slog.String("detail", err.Error()))
		shutdown(httpServer, rcache, pool, aw, logger)
		return err
	case <-sigCtx.Done():
		shutdown(httpServer, rcache, pool, aw, logger)
		return nil
	}
}

// shutdown reverses startup order: drain HTTP -> close Redis ->
// close pool -> drain async log queue, then a final synchronous shutdown line.
func shutdown(httpServer *http.Server, rcache *cache.Cache, pool poolCloser, aw *logx.AsyncWriter, logger *slog.Logger) {
	graceCtx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()

	_ = httpServer.Shutdown(graceCtx)
	_ = rcache.Close()
	pool.Close()

	// Final lifecycle line BEFORE draining the queue, so it is included in the
	// drain (lossless on shutdown).
	logger.LogAttrs(context.Background(), slog.LevelInfo, "shutdown_complete",
		slog.String("request_id", ""),
	)
	_ = aw.Close()
}

// poolCloser is the minimal pool interface shutdown needs.
type poolCloser interface{ Close() }

// gomemlimitString returns the effective soft memory limit as a base-10 byte
// string (spec/runtime-stats.md gomemlimit is a string). debug.SetMemoryLimit(-1)
// reads the current limit without changing it.
func gomemlimitString() string {
	return strconv.FormatInt(debug.SetMemoryLimit(-1), 10)
}
