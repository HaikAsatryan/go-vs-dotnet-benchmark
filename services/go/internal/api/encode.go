package api

import (
	"encoding/json"
	"net/http"
)

// contentTypeJSON matches ASP.NET's default for JSON results; both services
// emit the identical value on every JSON success body (NOTES-equivalence.md).
const contentTypeJSON = "application/json; charset=utf-8"

// encodeJSON marshals v and writes it with the given status and JSON content
// type. Used by the non-cached response paths (pricing, list, pdf-stub, health).
// json.Marshal (not Encoder) keeps the path allocation-honest and mirrors STJ.
func encodeJSON(w http.ResponseWriter, status int, v any) {
	body, err := json.Marshal(v)
	if err != nil {
		w.Header().Set("Content-Type", contentTypeJSON)
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", contentTypeJSON)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
