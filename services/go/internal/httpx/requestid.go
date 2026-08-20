package httpx

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

type ctxKey int

const requestIDKey ctxKey = iota

// headerRequestID is the inbound/outbound request-id header (spec/openapi.yaml).
const headerRequestID = "X-Request-Id"

// newUUIDv4 generates a random RFC 4122 version-4 UUID using crypto/rand, with
// no external dependency.
func newUUIDv4() string {
	var b [16]byte
	// crypto/rand.Read never returns an error on a healthy system; on the
	// astronomically unlikely failure we still emit a well-formed (all-zero)
	// id rather than panic mid-request.
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	var dst [36]byte
	hex.Encode(dst[0:8], b[0:4])
	dst[8] = '-'
	hex.Encode(dst[9:13], b[4:6])
	dst[13] = '-'
	hex.Encode(dst[14:18], b[6:8])
	dst[18] = '-'
	hex.Encode(dst[19:23], b[8:10])
	dst[23] = '-'
	hex.Encode(dst[24:36], b[10:16])
	return string(dst[:])
}

// RequestID is the outermost middleware: it adopts an inbound X-Request-Id when
// present, otherwise generates a UUIDv4, stashes it in the request context, and
// echoes it in the response header. The id NEVER enters a response body
// (excluded from JSON equivalence, spec/openapi.yaml).
func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get(headerRequestID)
		if id == "" {
			id = newUUIDv4()
		}
		w.Header().Set(headerRequestID, id)
		ctx := context.WithValue(r.Context(), requestIDKey, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// RequestIDFromContext returns the request id stored by the RequestID
// middleware, or "" if absent (lifecycle paths).
func RequestIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey).(string); ok {
		return v
	}
	return ""
}
