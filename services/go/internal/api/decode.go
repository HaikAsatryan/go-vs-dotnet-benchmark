package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
)

// errBadJSON signals a syntactically invalid / non-decodable body. Unknown
// fields are IGNORED (lenient, no DisallowUnknownFields), matching .NET STJ
// default (spec/openapi.yaml: unknown JSON fields ignored both sides).
var errBadJSON = errors.New("api: malformed JSON body")

// readBodyCapped reads the request body bounded by the pinned BODY_MAX_BYTES via
// http.MaxBytesReader (mirrors Kestrel MaxRequestBodySize). An oversized or
// unreadable body returns an error (the caller maps it to a 400).
func readBodyCapped(w http.ResponseWriter, r *http.Request, maxBytes int64) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	return io.ReadAll(r.Body)
}

// itoa renders an int64 in base 10 (Location header, no allocation surprises).
func itoa(n int64) string { return strconv.FormatInt(n, 10) }

// decodeJSON unmarshals raw JSON into dst (a typed struct) in a SINGLE parse.
// Absent-vs-present is not tracked: validation mirrors .NET DataAnnotations
// semantics on the bound zero values instead (absent string -> "" -> [Required];
// absent slice -> nil -> [Required]; absent number -> 0 -> [Range]), so no
// presence map (and no second parse) is needed: see validate.go. A non-object
// body or invalid JSON yields errBadJSON. Money fields decode straight into
// int64; a non-integer in a money field is a decode error -> 400 (upstream).
func decodeJSON(raw []byte, dst any) error {
	// .NET parity: STJ skips a UTF-8 BOM before parsing; encoding/json (and the
	// object gate below) would reject it.
	raw = bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})
	// .NET parity: a JSON `null` (or any non-object) body binds to a null DTO and
	// fails minimal-API body binding -> frozen 400 {"body":["is required"]}. Go's
	// Unmarshal would silently no-op on `null`, so require an object body first
	// (byte scan, not a parse).
	i := 0
	for i < len(raw) && (raw[i] == ' ' || raw[i] == '\t' || raw[i] == '\r' || raw[i] == '\n') {
		i++
	}
	if i == len(raw) || raw[i] != '{' {
		return errBadJSON
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		return errBadJSON
	}
	return nil
}
