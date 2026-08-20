package db

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Read statement bodies hand-copied VERBATIM from spec/db-access.sql for the
// hand-composed cache-miss read batch. Asserted byte-for-byte by
// sql_parity_test.go.
const (
	sqlGetInvoice = `SELECT id, customer_id, status, currency, total_minor, created_at
FROM invoices
WHERE id = $1`

	sqlGetInvoiceItems = `SELECT id, sku, description, qty, unit_price_minor, line_total_minor
FROM invoice_items
WHERE invoice_id = $1
ORDER BY id`
)

// ErrInvoiceNotFound is returned by GetInvoiceWithItems when the invoice id is
// absent (maps to 404; no negative caching, spec/cache.md).
var ErrInvoiceNotFound = errors.New("db: invoice not found")

// InvoiceRow mirrors the GetInvoice result columns.
type InvoiceRow struct {
	ID         int64
	CustomerID int64
	Status     string
	Currency   string
	TotalMinor int64
	CreatedAt  pgtype.Timestamptz
}

// ItemRow mirrors the GetInvoiceItems result columns (ORDER BY id ASC).
type ItemRow struct {
	ID             int64
	Sku            string
	Description    string
	Qty            int32
	UnitPriceMinor int64
	LineTotalMinor int64
}

// GetInvoiceWithItems is the cache-miss read path (spec/cache.md, spec/db-access.md
// Q1+Q2): ONE pool.SendBatch carrying GetInvoice + GetInvoiceItems in a single
// round-trip, no transaction. A missing invoice header returns
// ErrInvoiceNotFound (the endpoint then 404s; nothing is cached).
func GetInvoiceWithItems(ctx context.Context, pool *pgxpool.Pool, id int64) (InvoiceRow, []ItemRow, error) {
	batch := &pgx.Batch{}
	batch.Queue(sqlGetInvoice, id)
	batch.Queue(sqlGetInvoiceItems, id)

	br := pool.SendBatch(ctx, batch)
	defer br.Close()

	var inv InvoiceRow
	err := br.QueryRow().Scan(&inv.ID, &inv.CustomerID, &inv.Status, &inv.Currency, &inv.TotalMinor, &inv.CreatedAt)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return InvoiceRow{}, nil, ErrInvoiceNotFound
		}
		return InvoiceRow{}, nil, fmt.Errorf("db: get invoice: %w", err)
	}

	rows, err := br.Query()
	if err != nil {
		return InvoiceRow{}, nil, fmt.Errorf("db: get invoice items query: %w", err)
	}
	defer rows.Close()

	var items []ItemRow
	for rows.Next() {
		var it ItemRow
		if err := rows.Scan(&it.ID, &it.Sku, &it.Description, &it.Qty, &it.UnitPriceMinor, &it.LineTotalMinor); err != nil {
			return InvoiceRow{}, nil, fmt.Errorf("db: scan invoice item: %w", err)
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		return InvoiceRow{}, nil, fmt.Errorf("db: get invoice items rows: %w", err)
	}
	return inv, items, nil
}

// InvoiceExists is the pdf-stub 404 probe (spec/db-access.md): the Q1 header
// query, row presence checked without decoding any columns; the same statement
// text as the cache-miss read, so prepared-statement caches are shared.
func InvoiceExists(ctx context.Context, pool *pgxpool.Pool, id int64) (bool, error) {
	rows, err := pool.Query(ctx, sqlGetInvoice, id)
	if err != nil {
		return false, fmt.Errorf("db: invoice exists: %w", err)
	}
	defer rows.Close()
	exists := rows.Next()
	if err := rows.Err(); err != nil {
		return false, fmt.Errorf("db: invoice exists: %w", err)
	}
	return exists, nil
}
