package api

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"ledgerline/server/internal/db"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/metrics"
)

// buildInvoiceBody assembles the canonical InvoiceDto from DB rows and marshals
// it to the exact bytes the endpoint returns (serialize-then-cache pin: the same
// bytes are stored in Redis so a hit serves byte-equivalent output, spec/cache.md).
func buildInvoiceBody(inv db.InvoiceRow, items []db.ItemRow) ([]byte, error) {
	dto := InvoiceDto{
		ID:         inv.ID,
		CustomerID: inv.CustomerID,
		Status:     inv.Status,
		Currency:   inv.Currency,
		TotalMinor: inv.TotalMinor,
		CreatedAt:  FormatTimestamp(inv.CreatedAt.Time),
		Items:      make([]InvoiceItemDto, len(items)),
	}
	for i, it := range items {
		dto.Items[i] = InvoiceItemDto{
			ID:             it.ID,
			Sku:            it.Sku,
			Description:    it.Description,
			Qty:            it.Qty,
			UnitPriceMinor: it.UnitPriceMinor,
			LineTotalMinor: it.LineTotalMinor,
		}
	}
	return json.Marshal(dto)
}

// GetInvoice handles GET /invoices/{id}: Redis cache-aside -> PG fallback.
func (s *Server) GetInvoice(w http.ResponseWriter, r *http.Request) {
	s.sampler.Inc(metrics.EpGetInvoice)
	ctx := r.Context()

	id, ok := parsePositiveID(r.PathValue("id"))
	if !ok {
		httpx.WriteValidation(w, map[string][]string{"id": {msgIDPositive}})
		return
	}

	// 1. Cache lookup; hit returns the cached bytes verbatim.
	if body, hit, err := s.cache.Get(ctx, id); err != nil {
		s.internal(w, r, err)
		return
	} else if hit {
		writeJSONBytes(w, http.StatusOK, body)
		return
	}

	// 2. Miss: one batched read (Q1+Q2). Absent -> 404, NOT cached.
	inv, items, err := db.GetInvoiceWithItems(ctx, s.pool, id)
	if err != nil {
		if errors.Is(err, db.ErrInvoiceNotFound) {
			httpx.WriteNotFound(w)
			return
		}
		s.internal(w, r, err)
		return
	}

	body, err := buildInvoiceBody(inv, items)
	if err != nil {
		s.internal(w, r, err)
		return
	}

	// 3. Populate cache (positive only), then return.
	if err := s.cache.Set(ctx, id, body); err != nil {
		s.internal(w, r, err)
		return
	}
	writeJSONBytes(w, http.StatusOK, body)
}

// CreateInvoice handles POST /invoices: validate -> existence -> tx -> DEL -> log.
func (s *Server) CreateInvoice(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	if !s.requireJSONContentType(w, r) {
		return
	}

	raw, err := readBodyCapped(w, r, s.cfg.BodyMaxBytes)
	if err != nil {
		s.bodyReadFailed(w, r, err)
		return
	}

	var req CreateInvoiceRequest
	if derr := decodeJSON(raw, &req); derr != nil {
		s.invalidBody(w, r)
		return
	}

	// 400 (validation) BEFORE 404 (existence): validate first.
	if verrs := validateCreateInvoice(req); !verrs.empty() {
		httpx.WriteValidation(w, verrs)
		return
	}

	// Counted AFTER content-type/decode/validation, mirroring .NET, where binding and
	// the AddValidation filter short-circuit before the handler increments
	// (endpoint_calls therefore means "valid requests" on the POST endpoints, both sides).
	s.sampler.Inc(metrics.EpCreateInvoice)

	// 404: customer_id parses + valid but does not exist.
	exists, err := s.queries.CustomerExists(ctx, req.CustomerID)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	if !exists {
		httpx.WriteNotFound(w)
		return
	}

	// Compute totals server-side, build tx params.
	items := make([]db.CreateItem, len(req.Items))
	var total int64
	for i, it := range req.Items {
		lineTotal := int64(it.Qty) * it.UnitPriceMinor
		items[i] = db.CreateItem{
			Sku:            it.Sku,
			Description:    it.Description,
			Qty:            it.Qty,
			UnitPriceMinor: it.UnitPriceMinor,
			LineTotalMinor: lineTotal,
		}
		total += lineTotal
	}

	res, err := db.CreateInvoiceTx(ctx, s.pool, db.CreateParams{
		CustomerID: req.CustomerID,
		Status:     req.Status,
		Currency:   req.Currency,
		TotalMinor: total,
		Items:      items,
	})
	if err != nil {
		s.internal(w, r, err)
		return
	}

	// After commit: DEL the cache key, then domain log events (spec/db-access.md).
	if err := s.cache.Invalidate(ctx, res.ID); err != nil {
		s.internal(w, r, err)
		return
	}
	s.logger.LogAttrs(ctx, slog.LevelInfo, "invoice_created",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int64("customer_id", req.CustomerID),
		slog.Int64("invoice_id", res.ID),
		slog.Int("item_count", len(items)),
		slog.Int64("total_minor", total),
	)
	s.logger.LogAttrs(ctx, slog.LevelInfo, "cache_invalidated",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int64("invoice_id", res.ID),
	)

	// Build the 201 body from the same computed data + DB-generated id/created_at.
	dto := InvoiceDto{
		ID:         res.ID,
		CustomerID: req.CustomerID,
		Status:     req.Status,
		Currency:   req.Currency,
		TotalMinor: total,
		CreatedAt:  FormatTimestamp(res.CreatedAt.Time),
		Items:      make([]InvoiceItemDto, len(items)),
	}
	for i, it := range items {
		dto.Items[i] = InvoiceItemDto{
			ID:             res.ItemIDs[i],
			Sku:            it.Sku,
			Description:    it.Description,
			Qty:            it.Qty,
			UnitPriceMinor: it.UnitPriceMinor,
			LineTotalMinor: it.LineTotalMinor,
		}
	}
	body, err := json.Marshal(dto)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	w.Header().Set("Content-Type", contentTypeJSON)
	w.Header().Set("Location", "/invoices/"+itoa(res.ID))
	w.WriteHeader(http.StatusCreated)
	_, _ = w.Write(body)
}

// internal logs the frozen error/exception events and writes the 500 problem.
func (s *Server) internal(w http.ResponseWriter, r *http.Request, err error) {
	ctx := r.Context()
	rid := httpx.RequestIDFromContext(ctx)
	s.logger.LogAttrs(ctx, slog.LevelError, "request_error",
		slog.String("request_id", rid),
		slog.Int("status", http.StatusInternalServerError),
		slog.String("error_class", "internal"),
	)
	s.logger.LogAttrs(ctx, slog.LevelError, "exception",
		slog.String("request_id", rid),
		slog.String("detail", err.Error()),
	)
	httpx.WriteInternal(w)
}

// requireJSONContentType applies the FROZEN content-type gate (spec/openapi.yaml
// x-error-strings.unsupported_media_type): Content-Type up to the first ';',
// space/tab-trimmed, lowercased, must be "application/json" or a "+json" suffix
// with a "/" in it; parameters are never validated. Skipped for body-less
// requests (they fall through to body binding -> the frozen 400). Rejections
// are the frozen 415 with one request_error, and are not counted in
// endpoint_calls. The identical gate runs in .NET's MediaTypeGateMiddleware.
func (s *Server) requireJSONContentType(w http.ResponseWriter, r *http.Request) bool {
	if r.ContentLength == 0 {
		return true // no body: binding decides (frozen 400), same as .NET
	}
	if isJSONMediaType(r.Header.Get("Content-Type")) {
		return true
	}
	ctx := r.Context()
	s.logger.LogAttrs(ctx, slog.LevelError, "request_error",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int("status", http.StatusUnsupportedMediaType),
		slog.String("error_class", "validation"),
	)
	httpx.WriteUnsupportedMediaType(w)
	return false
}

// isJSONMediaType implements the frozen gate grammar. Deliberately NOT a full
// media-type parser: the same trivial string rule runs on both sides, so no
// parser-library disagreement can split the two services on adversarial input.
func isJSONMediaType(contentType string) bool {
	mt := contentType
	if i := strings.IndexByte(mt, ';'); i >= 0 {
		mt = mt[:i]
	}
	mt = strings.ToLower(strings.Trim(mt, " \t"))
	return mt == "application/json" ||
		(strings.HasSuffix(mt, "+json") && strings.Contains(mt, "/"))
}

// bodyReadFailed routes a body-read error: an OVERSIZED body is the frozen 413
// with NO request_error (spec/logging.md: Kestrel enforces .NET's limit below
// the point where the app can log, so both sides pin the access-line-only
// shape); any other read failure is the malformed-body 400.
func (s *Server) bodyReadFailed(w http.ResponseWriter, r *http.Request, err error) {
	var mbe *http.MaxBytesError
	if errors.As(err, &mbe) {
		httpx.WritePayloadTooLarge(w)
		return
	}
	s.invalidBody(w, r)
}

// invalidBody logs the frozen request_error (error_class="validation") and writes
// the frozen 400 {"body":["is required"]}. This is the undeserializable/malformed
// request-body path ONLY: an oversized/unreadable body or a non-object/invalid
// JSON body. It mirrors .NET's ProblemExceptionHandler, which logs request_error
// with error_class="validation" for the equivalent BadHttpRequestException /
// JsonException (spec/logging.md). Field-level validation 400s and 404s take
// httpx.WriteValidation / httpx.WriteNotFound directly and emit no request_error.
func (s *Server) invalidBody(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	s.logger.LogAttrs(ctx, slog.LevelError, "request_error",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int("status", http.StatusBadRequest),
		slog.String("error_class", "validation"),
	)
	httpx.WriteValidation(w, map[string][]string{"body": {msgRequired}})
}
