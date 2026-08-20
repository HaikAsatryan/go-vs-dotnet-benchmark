package api

import "strconv"

// parsePositiveID parses a path id. A non-integer or non-positive value is a 400
// (errors map key "id" -> id_positive); a well-formed positive id is returned.
// spec/openapi.yaml InvoiceId parameter.
func parsePositiveID(raw string) (int64, bool) {
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || n < 1 {
		return 0, false
	}
	return n, true
}

// parsePage parses the optional ?page= query value. Empty -> default 1, valid.
// Non-integer -> invalid with page_min. Below 1 -> page_min; above 100000 ->
// page_max. Returns (page, errMsg, ok): ok=false means a 400 with errMsg under
// key "page".
func parsePage(raw string) (int32, string, bool) {
	if raw == "" {
		return 1, "", true
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		// A non-integer page fails the minimum constraint first (400-before-404,
		// syntactic). Report page_min as the canonical message.
		return 0, msgPageMin, false
	}
	switch {
	case n < 1:
		return 0, msgPageMin, false
	case n > 100000:
		return 0, msgPageMax, false
	default:
		return int32(n), "", true
	}
}
