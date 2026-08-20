// Package cache implements the cache-aside contract (spec/cache.md) over
// go-redis v9: serialize-then-cache, hit returns cached bytes verbatim, no
// negative caching.
package cache

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"

	"ledgerline/server/internal/config"
)

// Cache wraps a go-redis client for the invoice cache-aside path.
type Cache struct {
	client *redis.Client
	ttl    time.Duration
}

// keyForInvoice builds the cache key "invoice:{id}" (base-10 id, no padding,
// spec/cache.md). strconv, not Sprintf: this runs on every GET/SET/DEL.
func keyForInvoice(id int64) string {
	return "invoice:" + strconv.FormatInt(id, 10)
}

// New constructs the Redis client from config (REDIS_ADDR, REDIS_POOL_MAX,
// CACHE_TTL_SECONDS).
func New(cfg config.Config) *Cache {
	client := redis.NewClient(&redis.Options{
		Addr:     cfg.RedisAddr,
		PoolSize: cfg.RedisPoolMax,
	})
	return &Cache{
		client: client,
		ttl:    time.Duration(cfg.CacheTTLSeconds) * time.Second,
	}
}

// Client exposes the underlying client (for PING readiness + PoolStats in
// /runtime-stats).
func (c *Cache) Client() *redis.Client { return c.client }

// Get returns the cached JSON body bytes for an invoice id. ok is false on a
// cache miss (redis.Nil). On a hit the bytes are returned VERBATIM (the value is
// the canonical GET body; no re-serialization, spec/cache.md).
func (c *Cache) Get(ctx context.Context, id int64) (body []byte, ok bool, err error) {
	b, err := c.client.Get(ctx, keyForInvoice(id)).Bytes()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("cache: get: %w", err)
	}
	return b, true, nil
}

// Set stores the canonical body bytes under invoice:{id} with the configured
// TTL (SET ... EX). Called only on a positive cache-miss load (no negative
// caching).
func (c *Cache) Set(ctx context.Context, id int64, body []byte) error {
	if err := c.client.Set(ctx, keyForInvoice(id), body, c.ttl).Err(); err != nil {
		return fmt.Errorf("cache: set: %w", err)
	}
	return nil
}

// Invalidate issues DEL invoice:{id} after a create (spec/cache.md write path).
func (c *Cache) Invalidate(ctx context.Context, id int64) error {
	if err := c.client.Del(ctx, keyForInvoice(id)).Err(); err != nil {
		return fmt.Errorf("cache: del: %w", err)
	}
	return nil
}

// Ping checks Redis liveness for /readyz.
func (c *Cache) Ping(ctx context.Context) error {
	return c.client.Ping(ctx).Err()
}

// Close shuts the client down (graceful shutdown order).
func (c *Cache) Close() error {
	return c.client.Close()
}
