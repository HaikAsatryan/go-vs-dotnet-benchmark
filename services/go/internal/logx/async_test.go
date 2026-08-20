package logx

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// syncBuffer is a concurrency-safe io.Writer collecting bytes for assertions.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}

// TestConcurrentInfoExactlyNLines: N concurrent logger.Info calls through the
// async writer produce exactly N lines after Close drains the queue (lossless,
// no drop, no duplication).
func TestConcurrentInfoExactlyNLines(t *testing.T) {
	const n = 5000
	sb := &syncBuffer{}
	aw := NewAsyncWriter(sb, 8192)
	logger := NewLogger(aw)

	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		go func(seq int) {
			defer wg.Done()
			logger.Info("test_event", slog.Int("seq", seq))
		}(i)
	}
	wg.Wait()
	if err := aw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	lines := nonEmptyLines(sb.String())
	if len(lines) != n {
		t.Fatalf("got %d lines, want %d", len(lines), n)
	}

	// Each line is valid JSON with the frozen schema keys, and every seq 0..n-1
	// appears exactly once (no loss, no duplication).
	seen := make([]bool, n)
	for _, ln := range lines {
		var rec map[string]any
		if err := json.Unmarshal([]byte(ln), &rec); err != nil {
			t.Fatalf("invalid JSON line %q: %v", ln, err)
		}
		if _, ok := rec["ts"]; !ok {
			t.Errorf("line missing ts: %q", ln)
		}
		if rec["level"] != "info" {
			t.Errorf("line level = %v, want info", rec["level"])
		}
		if rec["msg"] != "test_event" {
			t.Errorf("line msg = %v, want test_event", rec["msg"])
		}
		seqF, ok := rec["seq"].(float64)
		if !ok {
			t.Fatalf("seq not a number in %q", ln)
		}
		seq := int(seqF)
		if seq < 0 || seq >= n {
			t.Fatalf("seq %d out of range", seq)
		}
		if seen[seq] {
			t.Errorf("duplicate seq %d", seq)
		}
		seen[seq] = true
	}
}

// TestPostCloseSyncFallback: a Write after Close is written synchronously, not
// lost and not panicking on send-to-closed-channel.
func TestPostCloseSyncFallback(t *testing.T) {
	sb := &syncBuffer{}
	aw := NewAsyncWriter(sb, 4)
	if err := aw.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	n, err := aw.Write([]byte("late line\n"))
	if err != nil {
		t.Fatalf("post-close Write: %v", err)
	}
	if n != len("late line\n") {
		t.Fatalf("post-close Write n = %d", n)
	}
	if got := sb.String(); got != "late line\n" {
		t.Fatalf("post-close output = %q", got)
	}
}

// TestCloseIdempotent: Close may be called more than once.
func TestCloseIdempotent(t *testing.T) {
	aw := NewAsyncWriter(io.Discard, 8)
	if err := aw.Close(); err != nil {
		t.Fatalf("first Close: %v", err)
	}
	if err := aw.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
}

// TestBlockOnFullLossless: with a tiny queue and a throttled-then-released
// consumer, all lines still arrive (block-on-full, never drop).
func TestBlockOnFullLossless(t *testing.T) {
	const n = 2000
	// A writer that counts bytes/lines, simulating a slow stdout via no delay
	// but a tiny queue so producers must block on full.
	var lineCount atomic.Int64
	counter := writerFunc(func(p []byte) (int, error) {
		lineCount.Add(int64(bytes.Count(p, []byte{'\n'})))
		return len(p), nil
	})
	aw := NewAsyncWriter(counter, 1) // queue cap 1 forces frequent blocking
	logger := NewLogger(aw)

	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		go func(seq int) {
			defer wg.Done()
			logger.Info("e", slog.Int("seq", seq))
		}(i)
	}
	wg.Wait()
	_ = aw.Close()

	if got := lineCount.Load(); got != n {
		t.Fatalf("block-on-full lossless: got %d lines, want %d", got, n)
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }

func nonEmptyLines(s string) []string {
	var out []string
	for _, ln := range strings.Split(s, "\n") {
		if strings.TrimSpace(ln) != "" {
			out = append(out, ln)
		}
	}
	return out
}
