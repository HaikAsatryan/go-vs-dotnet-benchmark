package api

import (
	"reflect"
	"testing"
)

// validItem returns a CreateInvoiceItemRequest that passes all item rules.
func validItem() CreateInvoiceItemRequest {
	return CreateInvoiceItemRequest{Sku: "EL-0042", Description: "widget", Qty: 5, UnitPriceMinor: 1000}
}

// TestCreateInvoiceValid: a fully valid request yields no errors.
func TestCreateInvoiceValid(t *testing.T) {
	req := CreateInvoiceRequest{
		CustomerID: 1, Currency: "USD", Status: "draft",
		Items: []CreateInvoiceItemRequest{validItem(), validItem(), validItem()},
	}
	if v := validateCreateInvoice(req); !v.empty() {
		t.Fatalf("expected no errors, got %v", v)
	}
}

// TestCreateInvoiceAbsentFields mirrors .NET AddValidation on a zero-bound DTO
// (absent fields after the single-parse decode): strings/slices -> "is required";
// the non-nullable CustomerId long binds to 0 and fails [Range] instead.
func TestCreateInvoiceAbsentFields(t *testing.T) {
	req := CreateInvoiceRequest{} // all fields absent -> zero values
	v := validateCreateInvoice(req)

	if !hasMsg(v, "CustomerId", msgIDPositive) {
		t.Errorf("CustomerId expected %q (zero fails range, as on .NET); got %v", msgIDPositive, v["CustomerId"])
	}
	for _, f := range []string{"Currency", "Status", "Items"} {
		if !hasMsg(v, f, msgRequired) {
			t.Errorf("field %q missing %q; got %v", f, msgRequired, v[f])
		}
	}
}

// TestCreateInvoiceFrozenMessages: present-but-invalid fields emit the exact
// frozen per-rule messages; empty item strings hit [Required] first (.NET
// short-circuit), not the pattern/length message.
func TestCreateInvoiceFrozenMessages(t *testing.T) {
	req := CreateInvoiceRequest{
		CustomerID: 0,      // zero -> id_positive
		Currency:   "usd",  // lowercase -> currency_pattern
		Status:     "paid", // not in [draft, open] -> status_enum
		Items: []CreateInvoiceItemRequest{
			{Sku: "bad", Description: "", Qty: 0, UnitPriceMinor: -1},
		}, // 1 item -> items_create_count, plus item-level failures
	}
	v := validateCreateInvoice(req)

	checks := []struct {
		field, msg string
	}{
		{"CustomerId", msgIDPositive},
		{"Currency", msgCurrencyPattern},
		{"Status", msgStatusEnum},
		{"Items", msgItemsCreateCount},
		{"Items[0].Sku", msgSkuPattern},
		{"Items[0].Description", msgRequired}, // "" -> Required short-circuit, as on .NET
		{"Items[0].Qty", msgQtyRange},
		{"Items[0].UnitPriceMinor", msgUnitPriceRange},
	}
	for _, c := range checks {
		if !hasMsg(v, c.field, c.msg) {
			t.Errorf("field %q expected %q; got %v", c.field, c.msg, v[c.field])
		}
	}
}

// TestItemRequiredShortCircuit: empty sku/description emit ONLY "is required"
// (the pattern/length message must not co-fire), mirroring DataAnnotations'
// Required-first evaluation.
func TestItemRequiredShortCircuit(t *testing.T) {
	v := validationErrors{}
	validateCreateItem(v, 0, CreateInvoiceItemRequest{Sku: "", Description: "", Qty: 1, UnitPriceMinor: 0})

	for field, wrong := range map[string]string{
		"Items[0].Sku":         msgSkuPattern,
		"Items[0].Description": msgDescriptionLen,
	} {
		if !hasMsg(v, field, msgRequired) {
			t.Errorf("%s expected %q; got %v", field, msgRequired, v[field])
		}
		if hasMsg(v, field, wrong) {
			t.Errorf("%s must not also carry %q (Required short-circuits); got %v", field, wrong, v[field])
		}
	}
}

// TestDescriptionLengthUTF16: length is measured in UTF-16 code units, matching
// .NET string.Length: 200 BMP runes pass even at >200 bytes; 100 astral runes
// (2 units each) pass at exactly 200 units; 101 fail.
func TestDescriptionLengthUTF16(t *testing.T) {
	mk := func(desc string) validationErrors {
		v := validationErrors{}
		validateCreateItem(v, 0, CreateInvoiceItemRequest{Sku: "EL-0001", Description: desc, Qty: 1, UnitPriceMinor: 0})
		return v
	}

	twoHundredCyrillic := ""
	for range 200 {
		twoHundredCyrillic += "й" // 2 bytes, 1 UTF-16 unit
	}
	if v := mk(twoHundredCyrillic); hasMsg(v, "Items[0].Description", msgDescriptionLen) {
		t.Errorf("200 BMP runes (400 bytes) must pass, got %v", v)
	}

	astral := ""
	for range 100 {
		astral += "\U0001F600" // 4 bytes, 2 UTF-16 units
	}
	if v := mk(astral); hasMsg(v, "Items[0].Description", msgDescriptionLen) {
		t.Errorf("100 astral runes = 200 UTF-16 units must pass, got %v", v)
	}
	if v := mk(astral + "\U0001F600"); !hasMsg(v, "Items[0].Description", msgDescriptionLen) {
		t.Errorf("101 astral runes = 202 UTF-16 units must fail")
	}
}

// TestCreateInvoiceItemCountBoundaries: 3..8 inclusive is valid; 2 and 9 fail;
// an explicit empty list gets the count message, not "is required" (.NET
// [Required] passes on a non-null list).
func TestCreateInvoiceItemCountBoundaries(t *testing.T) {
	mk := func(n int) CreateInvoiceRequest {
		items := make([]CreateInvoiceItemRequest, n)
		for i := range items {
			items[i] = validItem()
		}
		return CreateInvoiceRequest{CustomerID: 1, Currency: "USD", Status: "open", Items: items}
	}

	for _, n := range []int{3, 8} {
		if v := validateCreateInvoice(mk(n)); hasMsg(v, "Items", msgItemsCreateCount) {
			t.Errorf("n=%d should pass item count", n)
		}
	}
	for _, n := range []int{0, 2, 9} {
		if v := validateCreateInvoice(mk(n)); !hasMsg(v, "Items", msgItemsCreateCount) {
			t.Errorf("n=%d should fail item count", n)
		}
	}
	// Empty non-nil list -> count message only, never "is required".
	if v := validateCreateInvoice(mk(0)); hasMsg(v, "Items", msgRequired) {
		t.Errorf("empty list must not emit %q", msgRequired)
	}
}

// TestQuoteValidation: exactly-50 rule + per-item messages + absent fields.
func TestQuoteValidation(t *testing.T) {
	mk := func(n int) QuoteRequest {
		items := make([]QuoteItemRequest, n)
		for i := range items {
			items[i] = QuoteItemRequest{Sku: "GR-0001", Qty: 1, UnitPriceMinor: 0}
		}
		return QuoteRequest{Currency: "USD", Items: items}
	}

	if v := validateQuote(mk(50)); !v.empty() {
		t.Errorf("50 items should pass, got %v", v)
	}
	if v := validateQuote(mk(49)); !hasMsg(v, "Items", msgItemsQuoteCount) {
		t.Errorf("49 items should fail quote count")
	}
	if v := validateQuote(mk(51)); !hasMsg(v, "Items", msgItemsQuoteCount) {
		t.Errorf("51 items should fail quote count")
	}

	// Absent currency/items -> "is required" (zero-bound DTO).
	v := validateQuote(QuoteRequest{})
	for _, f := range []string{"Currency", "Items"} {
		if !hasMsg(v, f, msgRequired) {
			t.Errorf("field %q missing %q; got %v", f, msgRequired, v[f])
		}
	}

	// Bad sku + qty in one item; empty sku short-circuits to "is required".
	bad := mk(50)
	bad.Items[0] = QuoteItemRequest{Sku: "ZZZ", Qty: 5000, UnitPriceMinor: 200_000_000}
	v = validateQuote(bad)
	if !hasMsg(v, "Items[0].Sku", msgSkuPattern) {
		t.Errorf("expected sku pattern error")
	}
	if !hasMsg(v, "Items[0].Qty", msgQtyRange) {
		t.Errorf("expected qty range error")
	}
	if !hasMsg(v, "Items[0].UnitPriceMinor", msgUnitPriceRange) {
		t.Errorf("expected unit price range error")
	}

	empty := mk(50)
	empty.Items[0].Sku = ""
	v = validateQuote(empty)
	if !hasMsg(v, "Items[0].Sku", msgRequired) || hasMsg(v, "Items[0].Sku", msgSkuPattern) {
		t.Errorf("empty sku must emit only %q; got %v", msgRequired, v["Items[0].Sku"])
	}
}

// TestDecodeJSONObjectGate: non-object bodies (null/array/scalar/garbage) are
// errBadJSON -> frozen {"body":["is required"]}, mirroring .NET body binding.
func TestDecodeJSONObjectGate(t *testing.T) {
	for _, raw := range []string{"null", "[1,2]", `"x"`, "42", "", "   ", "{bad"} {
		var req CreateInvoiceRequest
		if err := decodeJSON([]byte(raw), &req); err == nil {
			t.Errorf("body %q should be rejected", raw)
		}
	}
	var req CreateInvoiceRequest
	if err := decodeJSON([]byte(`  {"customer_id": 7}`), &req); err != nil || req.CustomerID != 7 {
		t.Errorf("object body should decode, got err=%v req=%+v", err, req)
	}
}

// TestFrozenMessageStrings pins the exact frozen strings from
// spec/openapi.yaml x-validation-messages (guards against accidental edits).
func TestFrozenMessageStrings(t *testing.T) {
	want := map[string]string{
		"required":           "is required",
		"sku_pattern":        "must match ^[A-Z]{2}-[0-9]{4}$",
		"currency_pattern":   "must match ^[A-Z]{3}$",
		"status_enum":        "must be one of: draft, open",
		"qty_range":          "must be between 1 and 1000",
		"unit_price_range":   "must be between 0 and 100000000",
		"items_create_count": "must contain between 3 and 8 items",
		"items_quote_count":  "must contain exactly 50 items",
		"description_length": "must be between 1 and 200 characters",
		"page_min":           "must be >= 1",
		"page_max":           "must be <= 100000",
		"id_positive":        "must be a positive integer",
	}
	got := map[string]string{
		"required":           msgRequired,
		"sku_pattern":        msgSkuPattern,
		"currency_pattern":   msgCurrencyPattern,
		"status_enum":        msgStatusEnum,
		"qty_range":          msgQtyRange,
		"unit_price_range":   msgUnitPriceRange,
		"items_create_count": msgItemsCreateCount,
		"items_quote_count":  msgItemsQuoteCount,
		"description_length": msgDescriptionLen,
		"page_min":           msgPageMin,
		"page_max":           msgPageMax,
		"id_positive":        msgIDPositive,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("frozen validation messages drifted:\n got  %v\n want %v", got, want)
	}
}

func hasMsg(v validationErrors, field, msg string) bool {
	for _, m := range v[field] {
		if m == msg {
			return true
		}
	}
	return false
}
