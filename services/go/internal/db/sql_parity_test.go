package db

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// specPath locates spec/db-access.sql relative to this test file
// (services/go/internal/db -> repo root is four levels up).
const specPath = "../../../../spec/db-access.sql"

// nameLineRe matches a sqlc query annotation: "-- name: <Name> :<type>".
var nameLineRe = regexp.MustCompile(`^--\s*name:\s*(\S+)\s+:\w+\s*$`)

// parseSpecQueries reads spec/db-access.sql and returns name -> normalized SQL
// body. The body is every line after the "-- name:" annotation up to (but not
// including) the next "-- name:" line or EOF, with blank lines trimmed at the
// ends and the trailing ';' stripped. This mirrors how sqlc and the hand-copied
// constants treat the statement body (no trailing semicolon).
func parseSpecQueries(t *testing.T) map[string]string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(specPath))
	if err != nil {
		t.Fatalf("read spec: %v", err)
	}
	lines := strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n")

	out := make(map[string]string)
	var curName string
	var curBody []string

	flush := func() {
		if curName == "" {
			return
		}
		body := strings.TrimSpace(strings.Join(curBody, "\n"))
		body = strings.TrimSpace(strings.TrimSuffix(body, ";"))
		out[curName] = body
	}

	for _, ln := range lines {
		if m := nameLineRe.FindStringSubmatch(strings.TrimSpace(ln)); m != nil {
			flush()
			curName = m[1]
			curBody = nil
			continue
		}
		if curName != "" {
			curBody = append(curBody, ln)
		}
	}
	flush()
	return out
}

// TestSQLParity asserts every hand-copied SQL constant in the db package matches
// the corresponding statement body in spec/db-access.sql byte-for-byte (the
// task's hand-copied-batch parity requirement).
func TestSQLParity(t *testing.T) {
	spec := parseSpecQueries(t)

	cases := []struct {
		queryName string
		constant  string
	}{
		{"InsertInvoice", sqlInsertInvoice},
		{"InsertInvoiceItem", sqlInsertInvoiceItem},
		{"UpdateCustomerBalance", sqlUpdateCustomerBalance},
		{"GetInvoice", sqlGetInvoice},
		{"GetInvoiceItems", sqlGetInvoiceItems},
	}

	for _, c := range cases {
		specBody, ok := spec[c.queryName]
		if !ok {
			t.Errorf("spec/db-access.sql has no query named %q", c.queryName)
			continue
		}
		if specBody != c.constant {
			t.Errorf("SQL parity mismatch for %s:\nspec:     %q\nconstant: %q", c.queryName, specBody, c.constant)
		}
	}
}

// TestSpecHasExpectedQueries guards against the spec gaining/renaming queries
// without the constants being updated.
func TestSpecHasExpectedQueries(t *testing.T) {
	spec := parseSpecQueries(t)
	want := []string{
		"GetInvoice", "GetInvoiceItems", "ListInvoicesByCustomer",
		"CustomerExists", "InsertInvoice", "InsertInvoiceItem", "UpdateCustomerBalance",
	}
	for _, w := range want {
		if _, ok := spec[w]; !ok {
			t.Errorf("spec/db-access.sql missing expected query %q", w)
		}
	}
}
