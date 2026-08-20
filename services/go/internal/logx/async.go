// Package logx provides the bounded async log writer (spec/logging.md async sink
// contract) and the slog.JSONHandler wiring on top.
package logx

import (
	"io"
	"sync"
)

// AsyncWriter is a bounded, lossless, block-on-full async log sink. slog formats
// each record inline on the calling goroutine and hands the formatted bytes to
// Write; Write copies them and performs a plain blocking channel send (never a
// select/default, which would drop). A single drain goroutine serializes writes
// to the underlying io.Writer (os.Stdout in production), so the hot path needs
// no output mutex.
//
// Contract (frozen, spec/logging.md):
//   - bounded queue, capacity = LOG_QUEUE_LEN (default 8192)
//   - block-on-full producer (lossless; mirrors .NET QueueFullMode=Wait)
//   - Close drains the queue fully, then returns (flush-on-shutdown)
//   - any Write after Close falls back to a direct synchronous out.Write so
//     late shutdown lines are never lost and never panic on send-to-closed-chan.
//
// Concurrency: a RWMutex separates senders from the closer. Write holds the read
// lock around the channel send, so many producers send concurrently; Close takes
// the write lock, guaranteeing no send is in flight when it closes the channel
// (no send-on-closed panic), then waits for the drain goroutine to finish.
type AsyncWriter struct {
	ch  chan []byte
	out io.Writer
	wg  sync.WaitGroup

	mu     sync.RWMutex // RLock: senders; Lock: closer
	closed bool
}

// NewAsyncWriter constructs the sink with the given queue capacity and starts
// its single drain goroutine. capacity <= 0 is coerced to 1.
func NewAsyncWriter(out io.Writer, capacity int) *AsyncWriter {
	if capacity <= 0 {
		capacity = 1
	}
	aw := &AsyncWriter{
		ch:  make(chan []byte, capacity),
		out: out,
	}
	aw.wg.Add(1)
	go aw.drain()
	return aw
}

func (aw *AsyncWriter) drain() {
	defer aw.wg.Done()
	for b := range aw.ch {
		_, _ = aw.out.Write(b)
	}
}

// Write copies p (slog reuses its buffer after the call returns) and blocks
// until the copy is queued. After Close it writes synchronously.
func (aw *AsyncWriter) Write(p []byte) (int, error) {
	b := make([]byte, len(p))
	copy(b, p)

	aw.mu.RLock()
	if aw.closed {
		aw.mu.RUnlock()
		// Post-close synchronous fallback: the queue is closed/drained, write
		// directly so the line is never lost.
		_, err := aw.out.Write(b)
		return len(p), err
	}
	// Hold the read lock across the send so Close (write lock) cannot close the
	// channel while a send is in flight. Block-on-full is the lossless guarantee.
	aw.ch <- b
	aw.mu.RUnlock()
	return len(p), nil
}

// Close drains the queue fully and returns once the drain goroutine has exited.
// Idempotent. Lossless on shutdown: every line already queued is written before
// Close returns.
func (aw *AsyncWriter) Close() error {
	aw.mu.Lock()
	if aw.closed {
		aw.mu.Unlock()
		return nil
	}
	aw.closed = true
	close(aw.ch)
	aw.mu.Unlock()

	aw.wg.Wait() // block until the queue is fully drained
	return nil
}
