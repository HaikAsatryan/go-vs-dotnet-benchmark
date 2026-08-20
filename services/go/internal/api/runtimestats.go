package api

import "net/http"

// RuntimeStats handles GET /runtime-stats: the identical-shape JSON of
// spec/runtime-stats.md (config echo, gc block, db_pool, redis_pool,
// endpoint_calls). Excluded from the workload mix and access log.
func (s *Server) RuntimeStats(w http.ResponseWriter, r *http.Request) {
	stats := s.sampler.Build(
		s.cfg,
		s.runtimeVersion,
		s.serviceVersion,
		s.dataLayer,
		s.gogc,
		s.gomemlimit,
	)
	encodeJSON(w, http.StatusOK, stats)
}
