package api

import "time"

// tsLayout produces RFC3339 UTC with EXACTLY 6 fractional digits and a literal
// 'Z' suffix. The fractional component uses ".000000" (zeros, not nines) so Go
// PADS to 6 digits and does NOT trim trailing zeros (the default RFC3339Nano /
// ".999999" forms trim, which would diverge from .NET). "Z07:00" renders as a
// bare "Z" for a UTC time. spec/openapi.yaml + spec/logging.md timestamp pin.
const tsLayout = "2006-01-02T15:04:05.000000Z07:00"

// FormatTimestamp renders t as the frozen wire/log timestamp string. The input
// is forced to UTC first so the offset element collapses to "Z".
func FormatTimestamp(t time.Time) string {
	return t.UTC().Format(tsLayout)
}
