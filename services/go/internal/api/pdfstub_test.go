package api

import (
	"strings"
	"testing"
)

// TestRenderPdfStubExactSize asserts the frozen algorithm yields exactly 32768
// bytes (spec/openapi.yaml pdf-stub description).
func TestRenderPdfStubExactSize(t *testing.T) {
	for _, id := range []int64{1, 42, 1234567, 5000000} {
		doc := renderPdfStub(id)
		if len(doc) != pdfStubSize {
			t.Errorf("renderPdfStub(%d) length = %d, want %d", id, len(doc), pdfStubSize)
		}
	}
}

// TestRenderPdfStubPrefix asserts the first line content for a known id.
func TestRenderPdfStubPrefix(t *testing.T) {
	doc := renderPdfStub(42)
	wantPrefix := "%PDFSTUB invoice=42 line=1\n%PDFSTUB invoice=42 line=2\n"
	if !strings.HasPrefix(string(doc), wantPrefix) {
		t.Errorf("prefix = %q, want prefix %q", string(doc[:min(len(doc), 64)]), wantPrefix)
	}
}

// TestRenderPdfStubLineFormat asserts every full line matches the frozen format
// and only the final line may be truncated to hit exactly 32768.
func TestRenderPdfStubLineFormat(t *testing.T) {
	const id = 7
	doc := renderPdfStub(id)
	if len(doc) != pdfStubSize {
		t.Fatalf("size = %d", len(doc))
	}
	s := string(doc)
	// Split keeps a trailing empty element if the doc ends with '\n'; the last
	// element is the (possibly truncated) final line otherwise.
	lines := strings.Split(s, "\n")
	for k := 1; k <= len(lines)-1; k++ {
		// Full lines 1..len-1 (the split element index k-1) must be complete.
		idx := k - 1
		if idx >= len(lines) {
			break
		}
		line := lines[idx]
		if line == "" {
			continue
		}
		want := "%PDFSTUB invoice=7 line=" + itoa(int64(k))
		// The final non-empty element may be a truncated prefix of its want.
		if idx == len(lines)-1 {
			if !strings.HasPrefix(want, line) && !strings.HasPrefix(line, want) {
				t.Errorf("final line %q not a prefix-consistent fragment of %q", line, want)
			}
			continue
		}
		if line != want {
			t.Errorf("line %d = %q, want %q", k, line, want)
		}
	}
}
