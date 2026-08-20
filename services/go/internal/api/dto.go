package api

// Wire DTOs (spec/openapi.yaml). JSON property names are snake_case EXACTLY as
// written in the spec (spec/openapi.yaml: "JSON property names: snake_case
// exactly as written here, both sides"). All money fields are int64 minor units;
// ids are int64 JSON integers; created_at is the frozen 6-digit-Z timestamp
// string.

// InvoiceItemDto is a line item in the Invoice response.
type InvoiceItemDto struct {
	ID             int64  `json:"id"`
	Sku            string `json:"sku"`
	Description    string `json:"description"`
	Qty            int32  `json:"qty"`
	UnitPriceMinor int64  `json:"unit_price_minor"`
	LineTotalMinor int64  `json:"line_total_minor"`
}

// InvoiceDto is the full invoice body (GET /invoices/{id}, POST /invoices 201).
// items are ORDER BY id ASC (order-significant in equivalence).
type InvoiceDto struct {
	ID         int64            `json:"id"`
	CustomerID int64            `json:"customer_id"`
	Status     string           `json:"status"`
	Currency   string           `json:"currency"`
	TotalMinor int64            `json:"total_minor"`
	CreatedAt  string           `json:"created_at"`
	Items      []InvoiceItemDto `json:"items"`
}

// InvoiceSummaryDto is one row of the list-page (no items).
type InvoiceSummaryDto struct {
	ID         int64  `json:"id"`
	CustomerID int64  `json:"customer_id"`
	Status     string `json:"status"`
	Currency   string `json:"currency"`
	TotalMinor int64  `json:"total_minor"`
	CreatedAt  string `json:"created_at"`
}

// InvoiceListPageDto is GET /customers/{id}/invoices. items are ORDER BY id DESC
// (order-significant). No total-count field by design.
type InvoiceListPageDto struct {
	CustomerID int64               `json:"customer_id"`
	Page       int32               `json:"page"`
	PageSize   int32               `json:"page_size"`
	Items      []InvoiceSummaryDto `json:"items"`
}

// CreateInvoiceItemRequest is one item in the POST /invoices body. Unknown JSON
// fields are ignored (lenient decode). Totals are computed server-side.
type CreateInvoiceItemRequest struct {
	Sku            string `json:"sku"`
	Description    string `json:"description"`
	Qty            int32  `json:"qty"`
	UnitPriceMinor int64  `json:"unit_price_minor"`
}

// CreateInvoiceRequest is the POST /invoices body.
type CreateInvoiceRequest struct {
	CustomerID int64                      `json:"customer_id"`
	Currency   string                     `json:"currency"`
	Status     string                     `json:"status"`
	Items      []CreateInvoiceItemRequest `json:"items"`
}

// PdfStubDto is the POST /invoices/{id}/pdf-stub response.
type PdfStubDto struct {
	InvoiceID    int64  `json:"invoice_id"`
	BytesWritten int32  `json:"bytes_written"`
	Path         string `json:"path"`
}

// QuoteItemRequest is one item in the POST /pricing/quote body.
type QuoteItemRequest struct {
	Sku            string `json:"sku"`
	Qty            int32  `json:"qty"`
	UnitPriceMinor int64  `json:"unit_price_minor"`
}

// QuoteRequest is the POST /pricing/quote body (exactly 50 items at the wire).
type QuoteRequest struct {
	Currency string             `json:"currency"`
	Items    []QuoteItemRequest `json:"items"`
}

// QuoteLineDto is one computed pricing line (input order, order-significant).
type QuoteLineDto struct {
	Sku                    string `json:"sku"`
	Category               string `json:"category"`
	Qty                    int32  `json:"qty"`
	UnitPriceMinor         int64  `json:"unit_price_minor"`
	CategorySurchargeMinor int64  `json:"category_surcharge_minor"`
	QtyDiscountMinor       int64  `json:"qty_discount_minor"`
	LineTotalMinor         int64  `json:"line_total_minor"`
}

// QuoteResponseDto is the POST /pricing/quote response.
type QuoteResponseDto struct {
	Currency           string         `json:"currency"`
	Lines              []QuoteLineDto `json:"lines"`
	SubtotalMinor      int64          `json:"subtotal_minor"`
	OrderDiscountMinor int64          `json:"order_discount_minor"`
	TotalMinor         int64          `json:"total_minor"`
}

// HealthDto is /healthz and /readyz.
type HealthDto struct {
	Status string `json:"status"`
}
