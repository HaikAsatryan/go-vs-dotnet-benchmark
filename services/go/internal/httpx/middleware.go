package httpx

import (
	"log/slog"
	"net/http"
	"time"
)

// statusRecorder wraps http.ResponseWriter to capture the response status code.
// Default status is 200 if WriteHeader is never called explicitly.
type statusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

func (sr *statusRecorder) WriteHeader(code int) {
	if !sr.wroteHeader {
		sr.status = code
		sr.wroteHeader = true
	}
	sr.ResponseWriter.WriteHeader(code)
}

func (sr *statusRecorder) Write(b []byte) (int, error) {
	if !sr.wroteHeader {
		sr.status = http.StatusOK
		sr.wroteHeader = true
	}
	return sr.ResponseWriter.Write(b)
}

// AccessLog wraps a handler with the per-request access line. routeTemplate is
// the MATCHED ROUTE TEMPLATE (e.g. "/invoices/{id}"), low-cardinality, never the
// raw URL (spec/logging.md). Exactly one line per request, msg="http_access".
// duration_ms is measured handler-entry -> response-write-complete via a
// monotonic clock. Apply ONLY to workload routes; health/runtime-stats are
// registered without it (excluded from access logging, spec/logging.md).
func AccessLog(logger *slog.Logger, routeTemplate string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sr := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sr, r)
		durationMs := float64(time.Since(start).Nanoseconds()) / 1e6
		logger.LogAttrs(r.Context(), slog.LevelInfo, "http_access",
			slog.String("request_id", RequestIDFromContext(r.Context())),
			slog.String("method", r.Method),
			slog.String("path", routeTemplate),
			slog.Int("status", sr.status),
			slog.Float64("duration_ms", durationMs),
		)
	})
}

// Recover guards every route: on a panic it logs an error line and returns a 500
// problem+json so a panic never kills the process mid-benchmark
// (spec/logging.md exception event).
func Recover(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				// net/http's deliberate abort sentinel: the server closes the
				// connection; re-panic instead of faking a logged 500.
				if rec == http.ErrAbortHandler {
					panic(rec)
				}
				rid := RequestIDFromContext(r.Context())
				logger.LogAttrs(r.Context(), slog.LevelError, "request_error",
					slog.String("request_id", rid),
					slog.Int("status", http.StatusInternalServerError),
					slog.String("error_class", "internal"),
				)
				logger.LogAttrs(r.Context(), slog.LevelError, "exception",
					slog.String("request_id", rid),
					slog.Any("detail", rec),
				)
				WriteInternal(w)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
