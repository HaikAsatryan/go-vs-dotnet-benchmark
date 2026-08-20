package api

import (
	"net/http"

	"ledgerline/server/internal/metrics"
)

// Healthz handles GET /healthz: liveness, static body, no DB/Redis touch. The
// HTTP-floor endpoint. Counted in endpoint_calls but excluded from access log.
func (s *Server) Healthz(w http.ResponseWriter, r *http.Request) {
	s.sampler.Inc(metrics.EpHealthz)
	encodeJSON(w, http.StatusOK, HealthDto{Status: "ok"})
}

// Readyz handles GET /readyz: readiness = DB pool reachable + Redis PING. Returns
// 200 {"status":"ok"} or 503 on failure. Used by compose healthcheck + warmup
// gate; not in the workload mix, not access-logged.
func (s *Server) Readyz(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	if err := s.pool.Ping(ctx); err != nil {
		encodeJSON(w, http.StatusServiceUnavailable, HealthDto{Status: "unavailable"})
		return
	}
	if err := s.cache.Ping(ctx); err != nil {
		encodeJSON(w, http.StatusServiceUnavailable, HealthDto{Status: "unavailable"})
		return
	}
	encodeJSON(w, http.StatusOK, HealthDto{Status: "ok"})
}
