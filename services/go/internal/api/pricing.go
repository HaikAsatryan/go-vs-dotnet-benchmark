package api

import (
	"log/slog"
	"net/http"

	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/metrics"
	"ledgerline/server/internal/pricing"
)

// Quote handles POST /pricing/quote: validate (exactly 50 items), run the frozen
// tiered pricing engine, return the computed lines + totals. No DB, no Redis.
func (s *Server) Quote(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	if !s.requireJSONContentType(w, r) {
		return
	}

	raw, err := readBodyCapped(w, r, s.cfg.BodyMaxBytes)
	if err != nil {
		s.bodyReadFailed(w, r, err)
		return
	}

	var req QuoteRequest
	if derr := decodeJSON(raw, &req); derr != nil {
		s.invalidBody(w, r)
		return
	}

	if verrs := validateQuote(req); !verrs.empty() {
		httpx.WriteValidation(w, verrs)
		return
	}

	// Counted after content-type/decode/validation (see CreateInvoice).
	s.sampler.Inc(metrics.EpPricingQuote)

	items := make([]pricing.Item, len(req.Items))
	for i, it := range req.Items {
		items[i] = pricing.Item{Sku: it.Sku, Qty: it.Qty, UnitPriceMinor: it.UnitPriceMinor}
	}
	q := pricing.Compute(req.Currency, items)

	lines := make([]QuoteLineDto, len(q.Lines))
	for i, l := range q.Lines {
		lines[i] = QuoteLineDto{
			Sku:                    l.Sku,
			Category:               l.Category,
			Qty:                    l.Qty,
			UnitPriceMinor:         l.UnitPriceMinor,
			CategorySurchargeMinor: l.CategorySurchargeMinor,
			QtyDiscountMinor:       l.QtyDiscountMinor,
			LineTotalMinor:         l.LineTotalMinor,
		}
	}

	s.logger.LogAttrs(ctx, slog.LevelInfo, "quote_computed",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int("item_count", len(items)),
		slog.Int64("total_minor", q.TotalMinor),
	)

	encodeJSON(w, http.StatusOK, QuoteResponseDto{
		Currency:           q.Currency,
		Lines:              lines,
		SubtotalMinor:      q.SubtotalMinor,
		OrderDiscountMinor: q.OrderDiscountMinor,
		TotalMinor:         q.TotalMinor,
	})
}
