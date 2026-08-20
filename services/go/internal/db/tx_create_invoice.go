package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Statement bodies hand-copied VERBATIM from spec/db-access.sql for the
// hand-composed create batch. A unit test (sql_parity_test.go) parses
// spec/db-access.sql and asserts each constant matches the spec body
// byte-for-byte. Do NOT edit these without updating the spec.
const (
	sqlInsertInvoice = `INSERT INTO invoices (customer_id, status, currency, total_minor)
VALUES ($1, $2, $3, $4)
RETURNING id, created_at`

	sqlInsertInvoiceItem = `INSERT INTO invoice_items (invoice_id, sku, description, qty, unit_price_minor, line_total_minor)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id`

	sqlUpdateCustomerBalance = `UPDATE customers
SET balance_minor = balance_minor + $2
WHERE id = $1`
)

// CreateItem is a single fully-computed item row for the create batch.
type CreateItem struct {
	Sku            string
	Description    string
	Qty            int32
	UnitPriceMinor int64
	LineTotalMinor int64
}

// CreateParams is the resolved input to the invoice-create transaction. Totals
// are already computed by the caller (the server computes line/total; the client
// never sends them).
type CreateParams struct {
	CustomerID int64
	Status     string
	Currency   string
	TotalMinor int64
	Items      []CreateItem
}

// CreateResult carries the DB-generated ids and created_at needed for the 201
// body: header id + created_at from the header RETURNING, per-item ids from the
// item RETURNINGs (all three data layers return real DB ids via RETURNING; see
// NOTES.md).
type CreateResult struct {
	ID        int64
	CreatedAt pgtype.Timestamptz
	ItemIDs   []int64
}

// CreateInvoiceTx executes the fairness-critical create transaction
// (spec/db-access.md), READ COMMITTED (PG default; neither side elevates).
//
// The item inserts bind the header id as a parameter, and pgx binds batch
// parameters at Queue() time, so it cannot propagate a RETURNING value between
// statements in one batch the way Npgsql's EF batch does. The header
// INSERT...RETURNING therefore runs as its own round-trip to obtain the IDENTITY
// id; the N item inserts and the balance update are then pipelined on ONE
// pgx.Batch. That mirrors EF Core's own shape (principal insert for the
// store-generated key, dependents batched), which is why it is the lowest
// cross-language divergence available here.
func CreateInvoiceTx(ctx context.Context, pool *pgxpool.Pool, p CreateParams) (CreateResult, error) {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return CreateResult{}, fmt.Errorf("db: begin create tx: %w", err)
	}
	// Rollback is a no-op after a successful Commit; safe to always defer.
	defer tx.Rollback(ctx)

	// Header insert: obtain the IDENTITY id + created_at (needed to bind item FKs
	// and to build the 201 body).
	var res CreateResult
	err = tx.QueryRow(ctx, sqlInsertInvoice, p.CustomerID, p.Status, p.Currency, p.TotalMinor).
		Scan(&res.ID, &res.CreatedAt)
	if err != nil {
		return CreateResult{}, fmt.Errorf("db: insert invoice: %w", err)
	}

	// One pipelined batch for the N item inserts + the balance update.
	batch := &pgx.Batch{}
	for _, it := range p.Items {
		batch.Queue(sqlInsertInvoiceItem, res.ID, it.Sku, it.Description, it.Qty, it.UnitPriceMinor, it.LineTotalMinor)
	}
	batch.Queue(sqlUpdateCustomerBalance, p.CustomerID, p.TotalMinor)

	br := tx.SendBatch(ctx, batch)
	// Drain every queued command (N item RETURNING id + 1 balance :exec) so
	// errors surface; Close must be called before Commit.
	res.ItemIDs = make([]int64, len(p.Items))
	for i := range p.Items {
		if err := br.QueryRow().Scan(&res.ItemIDs[i]); err != nil {
			_ = br.Close()
			return CreateResult{}, fmt.Errorf("db: create batch item insert: %w", err)
		}
	}
	if _, err := br.Exec(); err != nil {
		_ = br.Close()
		return CreateResult{}, fmt.Errorf("db: create batch balance update: %w", err)
	}
	if err := br.Close(); err != nil {
		return CreateResult{}, fmt.Errorf("db: create batch close: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return CreateResult{}, fmt.Errorf("db: commit create tx: %w", err)
	}
	return res, nil
}
