package pricing

import "testing"

// TestGoldenExampleA asserts the exact integers of worked Example A
// (spec/pricing-algorithm.md): electronics, mid qty tier.
func TestGoldenExampleA(t *testing.T) {
	q := Compute("USD", []Item{{Sku: "EL-0042", Qty: 10, UnitPriceMinor: 10000}})

	if len(q.Lines) != 1 {
		t.Fatalf("lines = %d, want 1", len(q.Lines))
	}
	l := q.Lines[0]
	if l.Category != "EL" {
		t.Fatalf("category = %q, want EL", l.Category)
	}
	assertInt(t, "category_surcharge_minor", l.CategorySurchargeMinor, 1500)
	assertInt(t, "qty_discount_minor", l.QtyDiscountMinor, 2000)
	assertInt(t, "line_total_minor", l.LineTotalMinor, 99500)
	assertInt(t, "subtotal_minor", q.SubtotalMinor, 99500)
	assertInt(t, "order_discount_minor", q.OrderDiscountMinor, 0)
	assertInt(t, "total_minor", q.TotalMinor, 99500)
}

// TestGoldenExampleB: unknown category (ZZ -> 0 bps), top qty tier.
func TestGoldenExampleB(t *testing.T) {
	q := Compute("USD", []Item{{Sku: "ZZ-1234", Qty: 200, UnitPriceMinor: 5000}})

	l := q.Lines[0]
	if l.Category != "ZZ" {
		t.Fatalf("category = %q, want ZZ", l.Category)
	}
	assertInt(t, "category_surcharge_minor", l.CategorySurchargeMinor, 0)
	assertInt(t, "qty_discount_minor", l.QtyDiscountMinor, 120000)
	assertInt(t, "line_total_minor", l.LineTotalMinor, 880000)
	assertInt(t, "subtotal_minor", q.SubtotalMinor, 880000)
	assertInt(t, "total_minor", q.TotalMinor, 880000)
}

// TestGoldenExampleC: two items, rounding (half-up) + near order threshold.
func TestGoldenExampleC(t *testing.T) {
	q := Compute("USD", []Item{
		{Sku: "HZ-0007", Qty: 3, UnitPriceMinor: 333},
		{Sku: "LX-9999", Qty: 100, UnitPriceMinor: 100000},
	})

	if len(q.Lines) != 2 {
		t.Fatalf("lines = %d, want 2", len(q.Lines))
	}

	// Item 1: HZ, qty 3.
	l1 := q.Lines[0]
	assertInt(t, "item1 category_surcharge_minor", l1.CategorySurchargeMinor, 50) // 49.95 -> half-up
	assertInt(t, "item1 qty_discount_minor", l1.QtyDiscountMinor, 0)
	assertInt(t, "item1 line_total_minor", l1.LineTotalMinor, 1049)

	// Item 2: LX, qty 100.
	l2 := q.Lines[1]
	assertInt(t, "item2 category_surcharge_minor", l2.CategorySurchargeMinor, 300000)
	assertInt(t, "item2 qty_discount_minor", l2.QtyDiscountMinor, 800000)
	assertInt(t, "item2 line_total_minor", l2.LineTotalMinor, 9500000)

	// Order.
	assertInt(t, "subtotal_minor", q.SubtotalMinor, 9501049)
	assertInt(t, "order_discount_minor", q.OrderDiscountMinor, 0)
	assertInt(t, "total_minor", q.TotalMinor, 9501049)
}

// TestOrderThresholdLadder exercises each order-discount tier boundary.
func TestOrderThresholdLadder(t *testing.T) {
	// Build a single GR (0 bps surcharge), qty 1 (0 discount) item whose
	// line_total equals the desired subtotal, so order tiers are isolated.
	cases := []struct {
		subtotal     int64
		wantDiscount int64
		wantTotal    int64
	}{
		{9_999_999, 0, 9_999_999},
		{10_000_000, 100_000, 9_900_000},
		{49_999_999, 500_000, 49_499_999},
		{50_000_000, 1_250_000, 48_750_000},
		{199_999_999, 5_000_000, 194_999_999},
		{200_000_000, 8_000_000, 192_000_000},
	}
	for _, c := range cases {
		q := Compute("USD", []Item{{Sku: "GR-0001", Qty: 1, UnitPriceMinor: c.subtotal}})
		if q.SubtotalMinor != c.subtotal {
			t.Fatalf("subtotal setup wrong: got %d want %d", q.SubtotalMinor, c.subtotal)
		}
		if q.OrderDiscountMinor != c.wantDiscount {
			t.Errorf("subtotal %d: order_discount = %d, want %d", c.subtotal, q.OrderDiscountMinor, c.wantDiscount)
		}
		if q.TotalMinor != c.wantTotal {
			t.Errorf("subtotal %d: total = %d, want %d", c.subtotal, q.TotalMinor, c.wantTotal)
		}
	}
}

// TestQtyTierBoundaries asserts the quantity discount tier lower bounds.
func TestQtyTierBoundaries(t *testing.T) {
	cases := []struct {
		qty int32
		bps int64
	}{
		{1, 0}, {9, 0}, {10, 200}, {49, 200}, {50, 500}, {99, 500},
		{100, 800}, {199, 800}, {200, 1200}, {1000, 1200},
	}
	for _, c := range cases {
		if got := qtyDiscountBps(c.qty); got != c.bps {
			t.Errorf("qtyDiscountBps(%d) = %d, want %d", c.qty, got, c.bps)
		}
	}
}

func assertInt(t *testing.T, name string, got, want int64) {
	t.Helper()
	if got != want {
		t.Errorf("%s = %d, want %d", name, got, want)
	}
}
