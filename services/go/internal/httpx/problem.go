package httpx

import (
	"encoding/json"
	"net/http"
)

// Problem is the RFC 7807 envelope (spec/openapi.yaml Problem schema). Field
// order in JSON is irrelevant; names/values are frozen. errors is present ONLY
// for 400 validation.
type Problem struct {
	Type   string              `json:"type"`
	Title  string              `json:"title"`
	Status int                 `json:"status"`
	Detail string              `json:"detail"`
	Errors map[string][]string `json:"errors,omitempty"`
}

// Frozen x-error-strings (spec/openapi.yaml). EXACT values, both services.
const (
	problemBaseValidation  = "https://ledgerline.test/problems/validation"
	problemBaseNotFound    = "https://ledgerline.test/problems/not-found"
	problemBaseInternal    = "https://ledgerline.test/problems/internal"
	problemBaseUnsupported = "https://ledgerline.test/problems/unsupported-media-type"
	problemBaseTooLarge    = "https://ledgerline.test/problems/payload-too-large"

	titleValidation  = "Validation failed"
	titleNotFound    = "Not found"
	titleInternal    = "Internal server error"
	titleUnsupported = "Unsupported media type"
	titleTooLarge    = "Payload too large"

	detailValidation  = "The request payload failed validation."
	detailNotFound    = "The requested resource does not exist."
	detailInternal    = "An unexpected error occurred."
	detailUnsupported = "The request content type must be application/json."
	detailTooLarge    = "The request body exceeds the configured limit."

	contentTypeProblem = "application/problem+json"
)

// writeProblem serializes p with the problem+json content type and status.
func writeProblem(w http.ResponseWriter, p Problem) {
	body, err := json.Marshal(p)
	if err != nil {
		// Marshaling a Problem cannot realistically fail; fall back to a bare 500.
		w.Header().Set("Content-Type", contentTypeProblem)
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", contentTypeProblem)
	w.WriteHeader(p.Status)
	_, _ = w.Write(body)
}

// WriteValidation emits the frozen 400 validation problem with the errors map
// (field path -> frozen messages). errs must be non-nil for a 400.
func WriteValidation(w http.ResponseWriter, errs map[string][]string) {
	writeProblem(w, Problem{
		Type:   problemBaseValidation,
		Title:  titleValidation,
		Status: http.StatusBadRequest,
		Detail: detailValidation,
		Errors: errs,
	})
}

// WriteNotFound emits the frozen 404 problem (no errors map).
func WriteNotFound(w http.ResponseWriter) {
	writeProblem(w, Problem{
		Type:   problemBaseNotFound,
		Title:  titleNotFound,
		Status: http.StatusNotFound,
		Detail: detailNotFound,
	})
}

// WriteUnsupportedMediaType emits the frozen 415 problem (no errors map).
func WriteUnsupportedMediaType(w http.ResponseWriter) {
	writeProblem(w, Problem{
		Type:   problemBaseUnsupported,
		Title:  titleUnsupported,
		Status: http.StatusUnsupportedMediaType,
		Detail: detailUnsupported,
	})
}

// WritePayloadTooLarge emits the frozen 413 problem (no errors map).
func WritePayloadTooLarge(w http.ResponseWriter) {
	writeProblem(w, Problem{
		Type:   problemBaseTooLarge,
		Title:  titleTooLarge,
		Status: http.StatusRequestEntityTooLarge,
		Detail: detailTooLarge,
	})
}

// WriteInternal emits the frozen 500 problem (no errors map).
func WriteInternal(w http.ResponseWriter) {
	writeProblem(w, Problem{
		Type:   problemBaseInternal,
		Title:  titleInternal,
		Status: http.StatusInternalServerError,
		Detail: detailInternal,
	})
}
