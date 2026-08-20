// Package money holds int64 minor-unit helpers. No float type ever appears in a
// money path (spec/openapi.yaml money convention).
package money

// BpsApply implements the FROZEN rounding rule from spec/pricing-algorithm.md:
//
//	bps_apply(amount, bps) = (amount * bps + 5000) / 10000   -- int64 truncating division
//
// The +5000 term gives round-half-up on non-negative values. No math.Round, no
// float/decimal intermediate. Overflow-safe for the documented domain
// (max amount*bps ~ 1.2e14, well inside int64).
func BpsApply(amount, bps int64) int64 {
	return (amount*bps + 5000) / 10000
}
