package httpx

import (
	"log/slog"
	"net/http"
)

// Routes holds the handlers wired by the api layer. Each is a plain
// http.HandlerFunc; the router applies the middleware chain and the
// access-log exclusions (spec/logging.md).
type Routes struct {
	GetInvoice     http.HandlerFunc // GET  /invoices/{id}
	CreateInvoice  http.HandlerFunc // POST /invoices
	PdfStub        http.HandlerFunc // POST /invoices/{id}/pdf-stub
	ListByCustomer http.HandlerFunc // GET  /customers/{id}/invoices
	Quote          http.HandlerFunc // POST /pricing/quote
	Healthz        http.HandlerFunc // GET  /healthz
	Readyz         http.HandlerFunc // GET  /readyz
	RuntimeStats   http.HandlerFunc // GET  /runtime-stats
}

// NewRouter builds the ServeMux with method+wildcard patterns (Go 1.22+).
// Workload routes get RequestID -> AccessLog -> Recover; health and
// runtime-stats get RequestID -> Recover only (excluded from access logging).
// The frozen route templates are the access-log "path" values AND the
// /runtime-stats endpoint_calls keys.
func NewRouter(logger *slog.Logger, r Routes) http.Handler {
	mux := http.NewServeMux()

	// Workload routes: access-logged. The string passed to AccessLog is the
	// frozen route template (low cardinality).
	workload := func(method, pattern, template string, h http.HandlerFunc) {
		mux.Handle(method+" "+pattern, AccessLog(logger, template, Recover(logger, h)))
	}
	// Health / instrumentation routes: NOT access-logged.
	excluded := func(method, pattern string, h http.HandlerFunc) {
		mux.Handle(method+" "+pattern, Recover(logger, h))
	}

	workload(http.MethodGet, "/invoices/{id}", "/invoices/{id}", r.GetInvoice)
	workload(http.MethodPost, "/invoices", "/invoices", r.CreateInvoice)
	workload(http.MethodPost, "/invoices/{id}/pdf-stub", "/invoices/{id}/pdf-stub", r.PdfStub)
	workload(http.MethodGet, "/customers/{id}/invoices", "/customers/{id}/invoices", r.ListByCustomer)
	workload(http.MethodPost, "/pricing/quote", "/pricing/quote", r.Quote)

	excluded(http.MethodGet, "/healthz", r.Healthz)
	excluded(http.MethodGet, "/readyz", r.Readyz)
	excluded(http.MethodGet, "/runtime-stats", r.RuntimeStats)

	// RequestID is the outermost wrapper over the whole mux.
	return RequestID(mux)
}
