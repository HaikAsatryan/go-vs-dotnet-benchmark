package api

import (
	"regexp"
	"testing"
	"time"
)

// tsRe matches RFC3339 UTC with EXACTLY 6 fractional digits and a literal Z.
var tsRe = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$`)

// TestFormatTimestampExactlySixDigitsZ asserts the format helper always emits 6
// fractional digits with a Z suffix, including the trailing-zero cases that Go's
// default RFC3339Nano would trim.
func TestFormatTimestampExactlySixDigitsZ(t *testing.T) {
	cases := []struct {
		name string
		in   time.Time
		want string
	}{
		{
			name: "whole second (trailing zeros must be padded, not trimmed)",
			in:   time.Date(2026, 6, 6, 0, 0, 0, 0, time.UTC),
			want: "2026-06-06T00:00:00.000000Z",
		},
		{
			name: "millisecond precision padded to 6",
			in:   time.Date(2026, 1, 2, 3, 4, 5, 123_000_000, time.UTC),
			want: "2026-01-02T03:04:05.123000Z",
		},
		{
			name: "microsecond precision exact",
			in:   time.Date(2026, 1, 2, 3, 4, 5, 123_456_000, time.UTC),
			want: "2026-01-02T03:04:05.123456Z",
		},
		{
			name: "nanosecond precision truncated to 6 digits",
			in:   time.Date(2026, 1, 2, 3, 4, 5, 123_456_789, time.UTC),
			want: "2026-01-02T03:04:05.123456Z",
		},
	}
	for _, c := range cases {
		got := FormatTimestamp(c.in)
		if got != c.want {
			t.Errorf("%s: FormatTimestamp = %q, want %q", c.name, got, c.want)
		}
		if !tsRe.MatchString(got) {
			t.Errorf("%s: %q does not match the 6-digit-Z shape", c.name, got)
		}
	}
}

// TestFormatTimestampForcesUTC asserts a non-UTC input is converted so the
// offset collapses to Z (never +00:00 or a real offset).
func TestFormatTimestampForcesUTC(t *testing.T) {
	loc := time.FixedZone("UTC+5", 5*3600)
	in := time.Date(2026, 6, 6, 5, 0, 0, 0, loc) // == 2026-06-06T00:00:00Z
	got := FormatTimestamp(in)
	want := "2026-06-06T00:00:00.000000Z"
	if got != want {
		t.Errorf("FormatTimestamp(non-UTC) = %q, want %q", got, want)
	}
}
