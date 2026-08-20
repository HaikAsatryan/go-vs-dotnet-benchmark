package logx

import (
	"io"
	"log/slog"
	"strings"
)

// tsLayout matches the wire timestamp format: RFC3339 UTC, exactly 6 fractional
// digits, literal 'Z'. Duplicated here (not imported from api) to keep logx
// dependency-free; the format string is the single frozen pin (spec/logging.md).
const tsLayout = "2006-01-02T15:04:05.000000Z07:00"

// replaceAttr rewrites slog's built-in attributes to the frozen line schema:
//   - "time" -> "ts" with the 6-digit UTC 'Z' format
//   - "level" value lowercased ("info"|"warn"|"error")
//
// msg and all per-event fields pass through unchanged.
func replaceAttr(_ []string, a slog.Attr) slog.Attr {
	switch a.Key {
	case slog.TimeKey:
		t := a.Value.Time()
		return slog.String("ts", t.UTC().Format(tsLayout))
	case slog.LevelKey:
		lvl := a.Value.Any().(slog.Level)
		return slog.String(slog.LevelKey, strings.ToLower(lvl.String()))
	default:
		return a
	}
}

// NewLogger builds the slog.Logger writing JSON lines to out (the AsyncWriter).
// Level floor is Info (debug disabled in benchmark builds, spec/logging.md).
func NewLogger(out io.Writer) *slog.Logger {
	h := slog.NewJSONHandler(out, &slog.HandlerOptions{
		Level:       slog.LevelInfo,
		ReplaceAttr: replaceAttr,
	})
	return slog.New(h)
}

// NewBootstrapLogger returns a synchronous JSON logger for the window before the
// async writer exists (early startup failures), using the identical line schema.
// Writes go straight to out (stderr in main).
func NewBootstrapLogger(out io.Writer) *slog.Logger {
	return NewLogger(out)
}
