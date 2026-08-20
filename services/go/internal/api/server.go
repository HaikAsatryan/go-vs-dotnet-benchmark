package api

import (
	"log/slog"
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"

	"ledgerline/server/internal/cache"
	"ledgerline/server/internal/config"
	"ledgerline/server/internal/db/gen"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/metrics"
)

// Server holds the handler dependencies. Plain constructor wiring, no container.
type Server struct {
	cfg     config.Config
	pool    *pgxpool.Pool
	queries *gen.Queries
	cache   *cache.Cache
	logger  *slog.Logger
	sampler *metrics.Sampler

	// Build metadata for /runtime-stats and the config line.
	runtimeVersion string
	serviceVersion string
	dataLayer      string
	gogc           string
	gomemlimit     string
}

// NewServer constructs the handler set.
func NewServer(
	cfg config.Config,
	pool *pgxpool.Pool,
	c *cache.Cache,
	logger *slog.Logger,
	sampler *metrics.Sampler,
	runtimeVersion, serviceVersion, dataLayer, gogc, gomemlimit string,
) *Server {
	return &Server{
		cfg:            cfg,
		pool:           pool,
		queries:        gen.New(pool),
		cache:          c,
		logger:         logger,
		sampler:        sampler,
		runtimeVersion: runtimeVersion,
		serviceVersion: serviceVersion,
		dataLayer:      dataLayer,
		gogc:           gogc,
		gomemlimit:     gomemlimit,
	}
}

// Routes returns the httpx.Routes wiring for the router.
func (s *Server) Routes() httpx.Routes {
	return httpx.Routes{
		GetInvoice:     s.GetInvoice,
		CreateInvoice:  s.CreateInvoice,
		PdfStub:        s.PdfStub,
		ListByCustomer: s.ListByCustomer,
		Quote:          s.Quote,
		Healthz:        s.Healthz,
		Readyz:         s.Readyz,
		RuntimeStats:   s.RuntimeStats,
	}
}

// writeJSONBytes writes pre-marshaled JSON bytes with the given status and the
// JSON content type. Used where the caller already has the canonical bytes (a
// cache value).
func writeJSONBytes(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", contentTypeJSON)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
