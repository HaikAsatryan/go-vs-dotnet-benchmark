using System.Text.Json.Serialization;
using Ledgerline.Dtos;

namespace Ledgerline.Json;

// STJ source-gen context. NO camelCase policy: the wire uses snake_case property names, carried
// by explicit [JsonPropertyName] on every DTO member (spec/openapi.yaml). Source-gen removes the
// reflection confounder from the benchmark.
[JsonSourceGenerationOptions(
   GenerationMode = JsonSourceGenerationMode.Default,
   PropertyNamingPolicy = JsonKnownNamingPolicy.Unspecified,
   DefaultIgnoreCondition = JsonIgnoreCondition.Never)]
[JsonSerializable(typeof(InvoiceDto))]
[JsonSerializable(typeof(InvoiceItemDto))]
[JsonSerializable(typeof(InvoiceSummaryDto))]
[JsonSerializable(typeof(InvoiceListPageDto))]
[JsonSerializable(typeof(PdfStubDto))]
[JsonSerializable(typeof(HealthDto))]
[JsonSerializable(typeof(QuoteRequest))]
[JsonSerializable(typeof(QuoteResponseDto))]
[JsonSerializable(typeof(QuoteLineDto))]
[JsonSerializable(typeof(CreateInvoiceRequest))]
[JsonSerializable(typeof(CreateInvoiceItem))]
public partial class LedgerJsonContext : JsonSerializerContext;
