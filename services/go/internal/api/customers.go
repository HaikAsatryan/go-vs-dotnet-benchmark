package api

import (
	"net/http"

	"ledgerline/server/internal/db/gen"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/metrics"
)

const pageSize = 20

// ListByCustomer handles GET /customers/{id}/invoices: a page of 20 summaries,
// ORDER BY id DESC, OFFSET (page-1)*20. A page beyond data returns 200 with empty
// items; customer existence is probed (Q3b) ONLY when the page is empty, then
// 404 if absent (spec/openapi.yaml, spec/db-access.md Q3/Q3b).
func (s *Server) ListByCustomer(w http.ResponseWriter, r *http.Request) {
	s.sampler.Inc(metrics.EpListInvoices)
	ctx := r.Context()

	// 400-before-404: validate path id and page before any existence probe.
	verrs := validationErrors{}
	id, ok := parsePositiveID(r.PathValue("id"))
	if !ok {
		verrs.add("id", msgIDPositive)
	}
	page, pageMsg, pageOK := parsePage(r.URL.Query().Get("page"))
	if !pageOK {
		verrs.add("page", pageMsg)
	}
	if !verrs.empty() {
		httpx.WriteValidation(w, verrs)
		return
	}

	offset := (int32(page) - 1) * pageSize
	rows, err := s.queries.ListInvoicesByCustomer(ctx, gen.ListInvoicesByCustomerParams{
		CustomerID: id,
		Offset:     offset,
	})
	if err != nil {
		s.internal(w, r, err)
		return
	}

	// Empty page: probe customer existence; absent -> 404.
	if len(rows) == 0 {
		exists, err := s.queries.CustomerExists(ctx, id)
		if err != nil {
			s.internal(w, r, err)
			return
		}
		if !exists {
			httpx.WriteNotFound(w)
			return
		}
	}

	items := make([]InvoiceSummaryDto, len(rows))
	for i, row := range rows {
		items[i] = InvoiceSummaryDto{
			ID:         row.ID,
			CustomerID: row.CustomerID,
			Status:     row.Status,
			Currency:   row.Currency,
			TotalMinor: row.TotalMinor,
			CreatedAt:  FormatTimestamp(row.CreatedAt.Time),
		}
	}

	encodeJSON(w, http.StatusOK, InvoiceListPageDto{
		CustomerID: id,
		Page:       page,
		PageSize:   pageSize,
		Items:      items,
	})
}
