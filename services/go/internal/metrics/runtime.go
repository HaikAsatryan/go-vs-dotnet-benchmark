// Package metrics samples runtime/metrics, pgxpool.Stat, and go-redis PoolStats
// for GET /runtime-stats (spec/runtime-stats.md), plus per-endpoint atomic call
// counters keyed by the frozen route templates.
package metrics

import (
	"math"
	"runtime/metrics"
	"sync"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

// Frozen endpoint_calls keys (spec/runtime-stats.md). Process-lifetime totals.
const (
	EpGetInvoice    = "GET /invoices/{id}"
	EpListInvoices  = "GET /customers/{id}/invoices"
	EpPricingQuote  = "POST /pricing/quote"
	EpCreateInvoice = "POST /invoices"
	EpPdfStub       = "POST /invoices/{id}/pdf-stub"
	EpHealthz       = "GET /healthz"
)

// endpointKeys is the frozen, ordered key set echoed in the response.
var endpointKeys = []string{
	EpGetInvoice, EpListInvoices, EpPricingQuote, EpCreateInvoice, EpPdfStub, EpHealthz,
}

// Sampler owns the runtime metric samples, pool handles, endpoint counters, and
// process start time.
type Sampler struct {
	pool  *pgxpool.Pool
	redis *redis.Client
	start time.Time

	counters map[string]*atomic.Int64

	// runtime/metrics machinery (sampled on demand).
	mu      sync.Mutex
	samples []metrics.Sample
	idx     map[string]int
}

// the exact non-deprecated runtime/metrics names (spec/runtime-stats.md field
// -> source mapping).
const (
	mPausesTotal  = "/sched/pauses/total/gc:seconds"
	mGCCycles     = "/gc/cycles/total:gc-cycles"
	mHeapAllocs   = "/gc/heap/allocs:bytes"
	mHeapGoal     = "/gc/heap/goal:bytes"
	mHeapObjects  = "/memory/classes/heap/objects:bytes"
	mClassTotal   = "/memory/classes/total:bytes"
	mHeapReleased = "/memory/classes/heap/released:bytes"
)

// NewSampler builds the sampler and initializes the runtime/metrics sample set
// and the endpoint counters.
func NewSampler(pool *pgxpool.Pool, rc *redis.Client) *Sampler {
	names := []string{
		mPausesTotal, mGCCycles, mHeapAllocs, mHeapGoal,
		mHeapObjects, mClassTotal, mHeapReleased,
	}
	samples := make([]metrics.Sample, len(names))
	idx := make(map[string]int, len(names))
	for i, n := range names {
		samples[i].Name = n
		idx[n] = i
	}
	counters := make(map[string]*atomic.Int64, len(endpointKeys))
	for _, k := range endpointKeys {
		counters[k] = &atomic.Int64{}
	}
	return &Sampler{
		pool:     pool,
		redis:    rc,
		start:    time.Now(),
		counters: counters,
		samples:  samples,
		idx:      idx,
	}
}

// Inc bumps the process-lifetime counter for an endpoint template. Unknown keys
// are ignored (the counter map is fixed to the frozen set).
func (s *Sampler) Inc(endpoint string) {
	if c, ok := s.counters[endpoint]; ok {
		c.Add(1)
	}
}

// uint64 reads one sample value; caller must hold s.mu.
func (s *Sampler) uint64(name string) uint64 {
	return s.samples[s.idx[name]].Value.Uint64()
}

// histogramSum approximates the cumulative total of a Float64Histogram metric
// as sum(count_i * bucket midpoint). /sched/pauses/total/gc:seconds is a
// histogram of individual pause durations (NOT a scalar), so the spec's
// pause_total_seconds is derived this way (same approach as client_golang).
// -Inf/+Inf edges fall back to the finite neighbor. Caller must hold s.mu.
func (s *Sampler) histogramSum(name string) float64 {
	h := s.samples[s.idx[name]].Value.Float64Histogram()
	if h == nil {
		return 0
	}
	var total float64
	for i, count := range h.Counts {
		if count == 0 {
			continue
		}
		lo, hi := h.Buckets[i], h.Buckets[i+1]
		mid := (lo + hi) / 2
		if math.IsInf(lo, -1) {
			mid = hi
		} else if math.IsInf(hi, +1) {
			mid = lo
		}
		total += float64(count) * mid
	}
	return total
}

// gcSample is one coherent extraction of the GC metrics.
type gcSample struct {
	cycles        int64
	pauseTotal    float64
	heapInUse     int64
	heapCommitted int64
	heapGoal      int64
	allocated     int64
}

// readGC refreshes the shared sample slice and extracts every value under ONE
// lock hold: metrics.Read is not concurrency-safe on a shared slice, and
// reading the samples outside the lock would race with a concurrent refresh.
func (s *Sampler) readGC() gcSample {
	s.mu.Lock()
	defer s.mu.Unlock()
	metrics.Read(s.samples)
	return gcSample{
		cycles:        int64(s.uint64(mGCCycles)),
		pauseTotal:    s.histogramSum(mPausesTotal),
		heapInUse:     int64(s.uint64(mHeapObjects)),
		heapCommitted: int64(s.uint64(mClassTotal)) - int64(s.uint64(mHeapReleased)),
		heapGoal:      int64(s.uint64(mHeapGoal)),
		allocated:     int64(s.uint64(mHeapAllocs)),
	}
}
