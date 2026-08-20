// Package db owns the pgxpool and the hand-composed invoice-create transaction
// that mixes sqlc-generated queries with a pgx.Batch (spec/db-access.md).
package db

import (
	"context"
	"fmt"
	"sync"

	"github.com/jackc/pgx/v5/pgxpool"

	"ledgerline/server/internal/config"
)

// NewPool builds a pgxpool from config. MinConns == MaxConns (min=max policy,
// spec/db-access.md): the pool neither cold-starts nor idle-prunes mid-run.
// MaxConnLifetime is pinned explicitly (env DB_CONN_LIFETIME). The exec mode is
// left at the pgx DEFAULT (QueryExecModeCacheStatement: auto server-side
// prepared-statement cache) per spec/db-access.md: pgx is never de-tuned.
func NewPool(ctx context.Context, cfg config.Config) (*pgxpool.Pool, error) {
	pcfg, err := pgxpool.ParseConfig(cfg.DSN())
	if err != nil {
		return nil, fmt.Errorf("db: parse pool config: %w", err)
	}
	pcfg.MaxConns = cfg.DBPoolMax
	pcfg.MinConns = cfg.DBPoolMin // == MaxConns
	pcfg.MaxConnLifetime = cfg.DBConnLifetime

	pool, err := pgxpool.NewWithConfig(ctx, pcfg)
	if err != nil {
		return nil, fmt.Errorf("db: new pool: %w", err)
	}
	return pool, nil
}

// WarmPool opens connections up to the pool's max by acquiring and pinging them
// in parallel, so the warmup gate sees db_pool.total == max
// (spec/db-access.md pool warmup; spec/slo.yaml pools_warm gate). All held
// connections are released back to the pool afterwards; with min=max they stay
// open.
func WarmPool(ctx context.Context, pool *pgxpool.Pool) error {
	max := int(pool.Config().MaxConns)
	conns := make([]*pgxpool.Conn, 0, max)
	var mu sync.Mutex
	var wg sync.WaitGroup
	errCh := make(chan error, max)

	for range max {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := pool.Acquire(ctx)
			if err != nil {
				errCh <- err
				return
			}
			if err := c.Ping(ctx); err != nil {
				c.Release()
				errCh <- err
				return
			}
			mu.Lock()
			conns = append(conns, c)
			mu.Unlock()
		}()
	}
	wg.Wait()
	for _, c := range conns {
		c.Release()
	}
	close(errCh)
	if err := <-errCh; err != nil {
		return fmt.Errorf("db: warm pool: %w", err)
	}
	return nil
}
