package api

import (
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"

	"ledgerline/server/internal/db"
	"ledgerline/server/internal/httpx"
	"ledgerline/server/internal/metrics"
)

// pdfStubSize is the FROZEN output size (spec/openapi.yaml pdf-stub description).
const pdfStubSize = 32768

// renderPdfStub builds the deterministic document per the FROZEN algorithm: ASCII
// lines "%PDFSTUB invoice=<id> line=<k>\n" for k = 1, 2, ... appended until the
// buffer reaches or exceeds 32768 bytes, then truncated to exactly 32768.
func renderPdfStub(invoiceID int64) []byte {
	buf := make([]byte, 0, pdfStubSize+64)
	for k := 1; len(buf) < pdfStubSize; k++ {
		buf = append(buf, "%PDFSTUB invoice="...)
		buf = strconv.AppendInt(buf, invoiceID, 10)
		buf = append(buf, " line="...)
		buf = strconv.AppendInt(buf, int64(k), 10)
		buf = append(buf, '\n')
	}
	return buf[:pdfStubSize]
}

// PdfStub handles POST /invoices/{id}/pdf-stub: render 32768 bytes in memory,
// buffered write to PDF_DIR/{id}.pdfstub (no fsync), 404 if the invoice does not
// exist. bytes_written is always 32768.
func (s *Server) PdfStub(w http.ResponseWriter, r *http.Request) {
	s.sampler.Inc(metrics.EpPdfStub)
	ctx := r.Context()

	id, ok := parsePositiveID(r.PathValue("id"))
	if !ok {
		httpx.WriteValidation(w, map[string][]string{"id": {msgIDPositive}})
		return
	}

	exists, err := db.InvoiceExists(ctx, s.pool, id)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	if !exists {
		httpx.WriteNotFound(w)
		return
	}

	doc := renderPdfStub(id)

	// One buffered write to the OS, no fsync/flush-to-disk call (spec). WriteFile is
	// create + one write + close: same syscall shape as .NET's File.WriteAllBytesAsync.
	fpath := filepath.Join(s.cfg.PDFDir, itoa(id)+".pdfstub")
	if err := os.WriteFile(fpath, doc, 0o644); err != nil {
		s.internal(w, r, err)
		return
	}

	s.logger.LogAttrs(ctx, slog.LevelInfo, "pdf_stub_written",
		slog.String("request_id", httpx.RequestIDFromContext(ctx)),
		slog.Int64("invoice_id", id),
		slog.Int("bytes_written", pdfStubSize),
	)

	// Response path is the canonical forward-slash form (equivalence-compared).
	respPath := s.cfg.PDFDir + "/" + itoa(id) + ".pdfstub"
	encodeJSON(w, http.StatusOK, PdfStubDto{
		InvoiceID:    id,
		BytesWritten: pdfStubSize,
		Path:         respPath,
	})
}
