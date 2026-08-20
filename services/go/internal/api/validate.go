package api

import (
	"fmt"
	"regexp"
)

// Frozen per-rule validation messages (spec/openapi.yaml x-validation-messages).
// Both services emit these EXACT strings.
const (
	msgRequired         = "is required"
	msgSkuPattern       = "must match ^[A-Z]{2}-[0-9]{4}$"
	msgCurrencyPattern  = "must match ^[A-Z]{3}$"
	msgStatusEnum       = "must be one of: draft, open"
	msgQtyRange         = "must be between 1 and 1000"
	msgUnitPriceRange   = "must be between 0 and 100000000"
	msgItemsCreateCount = "must contain between 3 and 8 items"
	msgItemsQuoteCount  = "must contain exactly 50 items"
	msgDescriptionLen   = "must be between 1 and 200 characters"
	msgPageMin          = "must be >= 1"
	msgPageMax          = "must be <= 100000"
	msgIDPositive       = "must be a positive integer"
)

var (
	skuRe      = regexp.MustCompile(`^[A-Z]{2}-[0-9]{4}$`)
	currencyRe = regexp.MustCompile(`^[A-Z]{3}$`)
)

// validationErrors accumulates field-path -> [messages]. The key format is the
// .NET source-generated AddValidation() emission, which is canonical
// (services/dotnet/NOTES-equivalence.md, frozen in spec/openapi.yaml):
// PascalCase CLR-style names ("CustomerId", "Currency", "Status", "Items");
// item-level fields use "Items[N].Field" with a 0-based index. Manual
// path/query keys stay lowercase ("id", "page"), matching .NET's manual
// validation. A field may carry multiple messages.
type validationErrors map[string][]string

func (v validationErrors) add(field, msg string) {
	v[field] = append(v[field], msg)
}

func (v validationErrors) empty() bool { return len(v) == 0 }

func itemField(i int, field string) string {
	return fmt.Sprintf("Items[%d].%s", i, field)
}

// utf16Len mirrors .NET string.Length (UTF-16 code units): astral-plane runes
// count as 2. [StringLength] measures this, so Go must too (byte length would
// diverge on multibyte input even when the workload is ASCII).
func utf16Len(s string) int {
	n := 0
	for _, r := range s {
		n++
		if r > 0xFFFF {
			n++
		}
	}
	return n
}

// validateCreateInvoice applies the CreateInvoiceRequest rules (spec/openapi.yaml
// CreateInvoiceRequest). Semantics MIRROR .NET AddValidation's DataAnnotations
// emission exactly (the frozen contract source), validating bound zero values
// from the single-parse decode (decode.go):
//   - strings: absent/null/"" all bind to "" -> [Required] "is required";
//     [Required] short-circuits the pattern/length check, as DataAnnotations does;
//   - slices: absent/null bind to nil -> [Required]; a present list with a wrong
//     count gets the count message (.NET [Min/MaxLength] skip null);
//   - numbers: absent binds to the zero value, which is range-checked (.NET long
//     CustomerId has no [Required]; default 0 fails [Range(1, max)]).
//
// Known residual divergence (documented, never exercised by the workload or the
// equivalence vectors): an EXPLICIT JSON null on a numeric field is a
// deserialization error on .NET (frozen 400 {"body":["is required"]}) but a
// no-op zero on Go (field-level range message instead).
func validateCreateInvoice(req CreateInvoiceRequest) validationErrors {
	v := validationErrors{}

	// customer_id: [Range(1, long.Max)] on a non-nullable long; absent -> 0.
	if req.CustomerID < 1 {
		v.add("CustomerId", msgIDPositive)
	}

	// currency: [Required] then ^[A-Z]{3}$.
	if req.Currency == "" {
		v.add("Currency", msgRequired)
	} else if !currencyRe.MatchString(req.Currency) {
		v.add("Currency", msgCurrencyPattern)
	}

	// status: [Required] then enum [draft, open].
	if req.Status == "" {
		v.add("Status", msgRequired)
	} else if req.Status != "draft" && req.Status != "open" {
		v.add("Status", msgStatusEnum)
	}

	// items: [Required] (absent/null -> nil) then count 3..8.
	if req.Items == nil {
		v.add("Items", msgRequired)
	} else if len(req.Items) < 3 || len(req.Items) > 8 {
		v.add("Items", msgItemsCreateCount)
	}

	// Per-item validation (every present item, regardless of count).
	for i, it := range req.Items {
		validateCreateItem(v, i, it)
	}
	return v
}

func validateCreateItem(v validationErrors, i int, it CreateInvoiceItemRequest) {
	// [Required] first, short-circuiting the format check (DataAnnotations order).
	if it.Sku == "" {
		v.add(itemField(i, "Sku"), msgRequired)
	} else if !skuRe.MatchString(it.Sku) {
		v.add(itemField(i, "Sku"), msgSkuPattern)
	}
	if it.Description == "" {
		v.add(itemField(i, "Description"), msgRequired)
	} else if utf16Len(it.Description) > 200 {
		v.add(itemField(i, "Description"), msgDescriptionLen)
	}
	if it.Qty < 1 || it.Qty > 1000 {
		v.add(itemField(i, "Qty"), msgQtyRange)
	}
	if it.UnitPriceMinor < 0 || it.UnitPriceMinor > 100_000_000 {
		v.add(itemField(i, "UnitPriceMinor"), msgUnitPriceRange)
	}
}

// validateQuote applies the QuoteRequest rules (spec/openapi.yaml QuoteRequest):
// currency pattern, exactly 50 items, per-item sku/qty/unit_price (no description
// on quote items). Same .NET-mirroring semantics as validateCreateInvoice.
func validateQuote(req QuoteRequest) validationErrors {
	v := validationErrors{}

	if req.Currency == "" {
		v.add("Currency", msgRequired)
	} else if !currencyRe.MatchString(req.Currency) {
		v.add("Currency", msgCurrencyPattern)
	}

	if req.Items == nil {
		v.add("Items", msgRequired)
	} else if len(req.Items) != 50 {
		v.add("Items", msgItemsQuoteCount)
	}

	for i, it := range req.Items {
		if it.Sku == "" {
			v.add(itemField(i, "Sku"), msgRequired)
		} else if !skuRe.MatchString(it.Sku) {
			v.add(itemField(i, "Sku"), msgSkuPattern)
		}
		if it.Qty < 1 || it.Qty > 1000 {
			v.add(itemField(i, "Qty"), msgQtyRange)
		}
		if it.UnitPriceMinor < 0 || it.UnitPriceMinor > 100_000_000 {
			v.add(itemField(i, "UnitPriceMinor"), msgUnitPriceRange)
		}
	}
	return v
}
