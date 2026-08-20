package money

import "testing"

// TestBpsApply asserts the FROZEN rounding expression
// (amount*bps + 5000) / 10000 against the worked-example intermediates and the
// half-up boundary (spec/pricing-algorithm.md).
func TestBpsApply(t *testing.T) {
	cases := []struct {
		amount, bps, want int64
	}{
		// Example A intermediates.
		{100000, 150, 1500},
		{100000, 200, 2000},
		// Example B.
		{1000000, 1200, 120000},
		// Example C: 49.95 -> 50 (half-up via +5000).
		{999, 500, 50},
		{10000000, 300, 300000},
		{10000000, 800, 800000},
		// Zero bps -> 0.
		{123456789, 0, 0},
		// Exact half rounds up: amount*bps = ...5000 boundary.
		// (1 * 5000 + 5000)/10000 = 10000/10000 = 1.
		{1, 5000, 1},
		// Just below half rounds down: (1*4999 + 5000)/10000 = 9999/10000 = 0.
		{1, 4999, 0},
	}
	for _, c := range cases {
		if got := BpsApply(c.amount, c.bps); got != c.want {
			t.Errorf("BpsApply(%d, %d) = %d, want %d", c.amount, c.bps, got, c.want)
		}
	}
}
