package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"ledgerline/server/internal/config"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/logx"
	"ledgerline/server/internal/metrics"
)

// newLogCapturingServer builds a Server wired with a real logger writing to buf
// and a no-op sampler. The body-decode and field-validation paths in
// CreateInvoice/Quote short-circuit before any DB/cache access, so the nil pool
// and cache are never touched on the paths exercised here.
func newLogCapturingServer(buf *bytes.Buffer) *Server {
	return &Server{
		cfg:     config.Config{BodyMaxBytes: 1 << 20},
		logger:  logx.NewLogger(buf),
		sampler: metrics.NewSampler(nil, nil),
	}
}

// requestErrors returns every captured request_error line as a decoded record.
func requestErrors(t *testing.T, out string) []map[string]any {
	t.Helper()
	var got []map[string]any
	for _, ln := range strings.Split(out, "\n") {
		if strings.TrimSpace(ln) == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(ln), &rec); err != nil {
			t.Fatalf("invalid JSON log line %q: %v", ln, err)
		}
		if rec["msg"] == "request_error" {
			got = append(got, rec)
		}
	}
	return got
}

// TestRequestErrorOnMalformedBody pins the cross-language contract: the
// undeserializable/malformed-body frozen-400 path emits exactly one request_error
// with error_class="validation" (mirroring .NET's ProblemExceptionHandler), and
// no msg="exception" line. Driven for both body-decoding endpoints.
func TestRequestErrorOnMalformedBody(t *testing.T) {
	cases := []struct {
		name    string
		method  string
		target  string
		body    string
		handler func(*Server) http.HandlerFunc
	}{
		{"create_malformed_json", http.MethodPost, "/invoices", "{bad",
			func(s *Server) http.HandlerFunc { return s.CreateInvoice }},
		{"create_non_object", http.MethodPost, "/invoices", "[1,2]",
			func(s *Server) http.HandlerFunc { return s.CreateInvoice }},
		{"create_null_body", http.MethodPost, "/invoices", "null",
			func(s *Server) http.HandlerFunc { return s.CreateInvoice }},
		{"quote_malformed_json", http.MethodPost, "/pricing/quote", "{bad",
			func(s *Server) http.HandlerFunc { return s.Quote }},
		{"quote_non_object", http.MethodPost, "/pricing/quote", `"x"`,
			func(s *Server) http.HandlerFunc { return s.Quote }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var buf bytes.Buffer
			s := newLogCapturingServer(&buf)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest(c.method, c.target, strings.NewReader(c.body))
			req.Header.Set("Content-Type", "application/json")
			c.handler(s)(rec, req)

			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			// Frozen body-400 envelope.
			var prob httpx.Problem
			if err := json.Unmarshal(rec.Body.Bytes(), &prob); err != nil {
				t.Fatalf("response not JSON: %v", err)
			}
			if got := prob.Errors["body"]; len(got) != 1 || got[0] != msgRequired {
				t.Fatalf("body errors = %v, want [%q]", got, msgRequired)
			}

			errs := requestErrors(t, buf.String())
			if len(errs) != 1 {
				t.Fatalf("got %d request_error lines, want exactly 1", len(errs))
			}
			if errs[0]["error_class"] != "validation" {
				t.Errorf("error_class = %v, want validation", errs[0]["error_class"])
			}
			if errs[0]["status"].(float64) != http.StatusBadRequest {
				t.Errorf("status = %v, want 400", errs[0]["status"])
			}
			if strings.Contains(buf.String(), `"msg":"exception"`) {
				t.Errorf("validation request_error must not co-emit an exception line:\n%s", buf.String())
			}
		})
	}
}

// TestUnsupportedMediaType: a POST body without a JSON content type (including a
// missing header) is the frozen 415 problem with one request_error line,
// mirroring .NET minimal-API binding (spec/openapi.yaml x-error-strings).
func TestUnsupportedMediaType(t *testing.T) {
	cases := []struct {
		name        string
		contentType string
	}{
		{"missing_header", ""},
		{"text_plain", "text/plain"},
		{"form", "application/x-www-form-urlencoded"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var buf bytes.Buffer
			s := newLogCapturingServer(&buf)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/invoices", strings.NewReader(`{}`))
			if c.contentType != "" {
				req.Header.Set("Content-Type", c.contentType)
			}
			s.CreateInvoice(rec, req)

			if rec.Code != http.StatusUnsupportedMediaType {
				t.Fatalf("status = %d, want 415", rec.Code)
			}
			var prob httpx.Problem
			if err := json.Unmarshal(rec.Body.Bytes(), &prob); err != nil {
				t.Fatalf("response not JSON: %v", err)
			}
			if prob.Type != "https://ledgerline.test/problems/unsupported-media-type" {
				t.Errorf("type = %q", prob.Type)
			}
			if prob.Detail != "The request content type must be application/json." {
				t.Errorf("detail = %q", prob.Detail)
			}
			if errs := requestErrors(t, buf.String()); len(errs) != 1 || errs[0]["status"].(float64) != http.StatusUnsupportedMediaType {
				t.Fatalf("want exactly 1 request_error with status 415, got: %v", errs)
			}
		})
	}

	// The frozen gate grammar (spec/openapi.yaml): parameters ignored, case
	// folded, "+json" suffixes need a "/", bare "+json" rejected.
	for ct, want := range map[string]bool{
		"application/json; charset=utf-8": true,
		"application/problem+json":        true,
		"APPLICATION/JSON":                true,
		" application/json ":              true,
		"application/json; charset":       true, // params never validated
		"+json":                           false,
		"text/json":                       false,
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/invoices", strings.NewReader(`{}`))
		req.Header.Set("Content-Type", ct)
		var buf bytes.Buffer
		newLogCapturingServer(&buf).CreateInvoice(rec, req)
		if got := rec.Code != http.StatusUnsupportedMediaType; got != want {
			t.Errorf("content type %q: accepted=%v, want %v", ct, got, want)
		}
	}

	// A BODY-LESS request skips the gate entirely: binding decides, and the
	// empty body is the frozen 400 {"body":["is required"]} (mirrors .NET,
	// where CanHaveBody=false bypasses the content-type check).
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/invoices", nil)
	var buf bytes.Buffer
	newLogCapturingServer(&buf).CreateInvoice(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("bodyless POST without Content-Type: status = %d, want 400", rec.Code)
	}
}

// TestOversizedBodyIs413WithoutRequestError: a body over BODY_MAX_BYTES is the
// frozen 413 problem with NO request_error line (spec/logging.md: access line
// only, both sides).
func TestOversizedBodyIs413WithoutRequestError(t *testing.T) {
	var buf bytes.Buffer
	s := newLogCapturingServer(&buf)

	big := strings.Repeat("x", int(s.cfg.BodyMaxBytes)+1024)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/invoices", strings.NewReader(`{"pad":"`+big+`"}`))
	req.Header.Set("Content-Type", "application/json")
	s.CreateInvoice(rec, req)

	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", rec.Code)
	}
	var prob httpx.Problem
	if err := json.Unmarshal(rec.Body.Bytes(), &prob); err != nil {
		t.Fatalf("response not JSON: %v", err)
	}
	if prob.Type != "https://ledgerline.test/problems/payload-too-large" {
		t.Errorf("type = %q", prob.Type)
	}
	if errs := requestErrors(t, buf.String()); len(errs) != 0 {
		t.Fatalf("413 must emit NO request_error, got: %v", errs)
	}
}

// TestNoRequestErrorOnFieldValidation: a syntactically valid object body that
// fails field-level rules still returns 400, but emits NO request_error (it is
// recorded via the access line's status only, spec/logging.md). Exercised for
// both body-decoding endpoints.
func TestNoRequestErrorOnFieldValidation(t *testing.T) {
	cases := []struct {
		name    string
		target  string
		handler func(*Server) http.HandlerFunc
	}{
		{"create", "/invoices", func(s *Server) http.HandlerFunc { return s.CreateInvoice }},
		{"quote", "/pricing/quote", func(s *Server) http.HandlerFunc { return s.Quote }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var buf bytes.Buffer
			s := newLogCapturingServer(&buf)

			rec := httptest.NewRecorder()
			// Empty object: valid JSON, decodes fine, fails field-level validation.
			req := httptest.NewRequest(http.MethodPost, c.target, strings.NewReader("{}"))
			req.Header.Set("Content-Type", "application/json")
			c.handler(s)(rec, req)

			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			if errs := requestErrors(t, buf.String()); len(errs) != 0 {
				t.Fatalf("field-validation 400 emitted %d request_error lines, want 0:\n%s", len(errs), buf.String())
			}
		})
	}
}

// TestNoRequestErrorOnNotFound: the 404 path writes via httpx.WriteNotFound, which
// is logger-free by construction; no request_error is emitted (the 404 is visible
// via the access line's status only, spec/logging.md).
func TestNoRequestErrorOnNotFound(t *testing.T) {
	rec := httptest.NewRecorder()
	httpx.WriteNotFound(rec)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	// The not-found writer takes no logger; assert the contract structurally.
	var prob httpx.Problem
	if err := json.Unmarshal(rec.Body.Bytes(), &prob); err != nil {
		t.Fatalf("response not JSON: %v", err)
	}
	if prob.Errors != nil {
		t.Fatalf("404 problem must carry no errors map, got %v", prob.Errors)
	}
}
