// Package pricing implements the frozen tiered pricing algorithm
// (spec/pricing-algorithm.md). Pure int64 integer arithmetic, branch-heavy,
// map lookups, per-item category parse. No floats anywhere.
package pricing

import "ledgerline/server/internal/money"

// Item is a single validated quote input line. sku is assumed to already match
// ^[A-Z]{2}-[0-9]{4}$ (validated at the API boundary before pricing runs).
type Item struct {
	Sku            string
	Qty            int32
	UnitPriceMinor int64
}

// Line is the computed per-item result, in input order.
type Line struct {
	Sku                    string
	Category               string
	Qty                    int32
	UnitPriceMinor         int64
	CategorySurchargeMinor int64
	QtyDiscountMinor       int64
	LineTotalMinor         int64
}

// Quote is the full computed result.
type Quote struct {
	Currency           string
	Lines              []Line
	SubtotalMinor      int64
	OrderDiscountMinor int64
	TotalMinor         int64
}

// surchargeBpsByCategory is the frozen category -> surcharge_bps map
// (spec/pricing-algorithm.md). Unknown category -> 0 bps (default bucket, not an
// error) via the comma-ok lookup below.
var surchargeBpsByCategory = map[string]int64{
	"EL": 150,
	"GR": 0,
	"HZ": 500,
	"LX": 300,
	"DG": 25,
	"FU": 120,
	"MD": 80,
	"AP": 60,
	"BK": 10,
	"TL": 90,
}

// qtyDiscountBps returns the bulk-discount bps for a quantity tier (frozen;
// lower bound inclusive). spec/pricing-algorithm.md.
func qtyDiscountBps(qty int32) int64 {
	return switchQtyTier(int64(qty))
}

func switchQtyTier(qty int64) int64 {
	switch {
	case qty >= 200:
		return 1200
	case qty >= 100:
		return 800
	case qty >= 50:
		return 500
	case qty >= 10:
		return 200
	default: // 1 - 9
		return 0
	}
}

// orderDiscountBps returns the order-level threshold discount bps (frozen;
// lower bound inclusive). spec/pricing-algorithm.md.
func orderDiscountBps(subtotal int64) int64 {
	switch {
	case subtotal >= 200_000_000:
		return 400
	case subtotal >= 50_000_000:
		return 250
	case subtotal >= 10_000_000:
		return 100
	default: // 0 - 9,999,999
		return 0
	}
}

// Compute evaluates the frozen algorithm over the input items in input order.
// currency is echoed verbatim.
func Compute(currency string, items []Item) Quote {
	lines := make([]Line, len(items))
	var subtotal int64

	for i, it := range items {
		// 1. line_subtotal = qty * unit_price_minor
		lineSubtotal := int64(it.Qty) * it.UnitPriceMinor

		// 2. category = sku[0:2]; surcharge_bps = MAP[category] else 0
		category := it.Sku[0:2]
		surchargeBps := surchargeBpsByCategory[category] // zero value (0) when absent

		// 3. category_surcharge_minor
		categorySurcharge := money.BpsApply(lineSubtotal, surchargeBps)

		// 4. qty_discount_bps = TIER(qty)
		// 5. qty_discount_minor
		qtyDiscount := money.BpsApply(lineSubtotal, qtyDiscountBps(it.Qty))

		// 6. line_total_minor = line_subtotal + category_surcharge - qty_discount
		lineTotal := lineSubtotal + categorySurcharge - qtyDiscount

		lines[i] = Line{
			Sku:                    it.Sku,
			Category:               category,
			Qty:                    it.Qty,
			UnitPriceMinor:         it.UnitPriceMinor,
			CategorySurchargeMinor: categorySurcharge,
			QtyDiscountMinor:       qtyDiscount,
			LineTotalMinor:         lineTotal,
		}
		subtotal += lineTotal
	}

	// Order-level threshold discount (after all items).
	orderDiscount := money.BpsApply(subtotal, orderDiscountBps(subtotal))
	total := subtotal - orderDiscount

	return Quote{
		Currency:           currency,
		Lines:              lines,
		SubtotalMinor:      subtotal,
		OrderDiscountMinor: orderDiscount,
		TotalMinor:         total,
	}
}
