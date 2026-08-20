// Package config parses the environment surface into a single immutable Config,
// with the effective values echoed once at startup.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config is the resolved effective configuration. All HTTP server timeouts are
// pinned constants (not env-driven) but echoed in the config line for fairness.
type Config struct {
	Port string

	PGHost     string
	PGPort     string
	PGDB       string
	PGUser     string
	PGPassword string

	DBPoolMax       int32
	DBPoolMin       int32 // = DBPoolMax (min=max policy, spec/db-access.md)
	DBConnLifetime  time.Duration
	RedisAddr       string
	RedisPoolMax    int
	CacheTTLSeconds int

	LogQueueLen  int
	BodyMaxBytes int64

	PDFDir string

	// Pinned http.Server timeouts (echoed for parity with Kestrel limits).
	ReadHeaderTimeout time.Duration
	ReadTimeout       time.Duration
	WriteTimeout      time.Duration
	IdleTimeout       time.Duration
}

func getenv(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func getenvInt(key string, def int) (int, error) {
	v := getenv(key, "")
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, fmt.Errorf("config: %s=%q is not an integer: %w", key, v, err)
	}
	return n, nil
}

// Load resolves every env var with the locked defaults. It returns an error on a
// malformed integer so a misconfig aborts startup visibly.
func Load() (Config, error) {
	dbPoolMax, err := getenvInt("DB_POOL_MAX", 24)
	if err != nil {
		return Config{}, err
	}
	// Same env surface as .NET's LedgerConfig: DB_POOL_MIN defaults to DB_POOL_MAX.
	// min=max is POLICY (spec/db-access.md): the idle-prune behaviors of pgxpool and
	// Npgsql differ (30 min vs 300 s defaults), so a min below max would shrink the
	// two pools differently mid-run. Both services refuse to start on a mismatch.
	dbPoolMin, err := getenvInt("DB_POOL_MIN", dbPoolMax)
	if err != nil {
		return Config{}, err
	}
	if dbPoolMin != dbPoolMax {
		return Config{}, fmt.Errorf(
			"config: DB_POOL_MIN=%d != DB_POOL_MAX=%d (min=max policy, spec/db-access.md)",
			dbPoolMin, dbPoolMax)
	}
	connLifetimeSecs, err := getenvInt("DB_CONN_LIFETIME", 3600)
	if err != nil {
		return Config{}, err
	}
	redisPoolMax, err := getenvInt("REDIS_POOL_MAX", 16)
	if err != nil {
		return Config{}, err
	}
	cacheTTL, err := getenvInt("CACHE_TTL_SECONDS", 300)
	if err != nil {
		return Config{}, err
	}
	logQueueLen, err := getenvInt("LOG_QUEUE_LEN", 8192)
	if err != nil {
		return Config{}, err
	}
	bodyMax, err := getenvInt("BODY_MAX_BYTES", 65536)
	if err != nil {
		return Config{}, err
	}

	return Config{
		Port:       getenv("PORT", "8080"),
		PGHost:     getenv("PG_HOST", "localhost"),
		PGPort:     getenv("PG_PORT", "5432"),
		PGDB:       getenv("PG_DB", "ledgerline"),
		PGUser:     getenv("PG_USER", "ledger"),
		PGPassword: getenv("PG_PASSWORD", "ledger"),

		DBPoolMax:       int32(dbPoolMax),
		DBPoolMin:       int32(dbPoolMin),
		DBConnLifetime:  time.Duration(connLifetimeSecs) * time.Second,
		RedisAddr:       getenv("REDIS_ADDR", "localhost:6379"),
		RedisPoolMax:    redisPoolMax,
		CacheTTLSeconds: cacheTTL,

		LogQueueLen:  logQueueLen,
		BodyMaxBytes: int64(bodyMax),

		PDFDir: getenv("PDF_DIR", "/data/pdf"),

		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}, nil
}

// DSN builds the libpq-style connection string consumed by pgxpool.ParseConfig.
func (c Config) DSN() string {
	return fmt.Sprintf(
		"host=%s port=%s dbname=%s user=%s password=%s",
		c.PGHost, c.PGPort, c.PGDB, c.PGUser, c.PGPassword,
	)
}
