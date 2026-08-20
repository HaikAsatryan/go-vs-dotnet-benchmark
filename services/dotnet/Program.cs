using Ledgerline.Cache;
using Ledgerline.Config;
using Ledgerline.Data;
using Ledgerline.Endpoints;
using Ledgerline.Infrastructure;
using Ledgerline.Json;
using Ledgerline.Logging;
using Ledgerline.Observability;
using Ledgerline.Pricing;
using Ledgerline.Problems;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Console;
using Npgsql;
using StackExchange.Redis;

var builder = WebApplication.CreateSlimBuilder(args);

var cfg = LedgerConfig.FromEnvironment();
builder.Services.AddSingleton(cfg);

builder.WebHost.ConfigureKestrel(k =>
{
   k.Limits.MaxRequestBodySize = cfg.BodyMaxBytes;   // BODY_MAX_BYTES (== Go MaxBytesReader)
   // Mirror the Go http.Server timeouts where Kestrel has an analog:
   // ReadHeaderTimeout 5s, IdleTimeout 120s. Kestrel has no absolute read/write
   // deadline (Go's ReadTimeout/WriteTimeout 30s): documented platform difference.
   k.Limits.RequestHeadersTimeout = TimeSpan.FromSeconds(5);
   k.Limits.KeepAliveTimeout = TimeSpan.FromSeconds(120);
   k.AddServerHeader = false;
});
builder.WebHost.UseUrls($"http://0.0.0.0:{cfg.Port}");

// Logging: custom flat-JSON console formatter (frozen schema), block-on-full bounded queue.
builder.Logging.ClearProviders();
builder.Logging.AddConsole(o => o.FormatterName = LedgerConsoleFormatter.FormatterName);
builder.Logging.AddConsoleFormatter<LedgerConsoleFormatter, ConsoleFormatterOptions>();
builder.Logging.Services.Configure<ConsoleLoggerOptions>(o =>
{
   o.QueueFullMode = ConsoleLoggerQueueFullMode.Wait;   // block-on-full, mirrors Go (lossless)
   o.MaxQueueLength = cfg.LogQueueLen;                  // LOG_QUEUE_LEN (8192)
});
builder.Logging.SetMinimumLevel(LogLevel.Information);
// spec/logging.md: no non-contract lines during measured traffic. Without this filter the
// framework emits ~6 Information lines per request (Hosting.Diagnostics request start/finish,
// EndpointMiddleware, HttpResults) through the same bounded queue; a measured fairness bug.
// Warning floor for framework categories is also the stock ASP.NET template default.
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
// EF Core logs "Executed DbCommand" (full SQL text) at Information PER QUERY:
// thousands of non-contract lines through the same bounded blocking queue
// during measured traffic (Go logs none): a measured fairness bug, same class
// as the AspNetCore filter above. spec/logging.md: contract lines only.
builder.Logging.AddFilter("Microsoft.EntityFrameworkCore", LogLevel.Warning);
// Hosting.Lifetime emits 5 Information lines ("Now listening on", "Application
// started", ...) at startup/shutdown: outside measured windows, but they are
// non-contract lines on one side only (Go emits none). spec/logging.md: the
// contract lines are the ONLY lines. Readiness is health-check-based, so
// nothing consumes these.
builder.Logging.AddFilter("Microsoft.Hosting.Lifetime", LogLevel.Warning);

// JSON: source-gen context (snake_case via explicit [JsonPropertyName]).
builder.Services.ConfigureHttpJsonOptions(o =>
   o.SerializerOptions.TypeInfoResolverChain.Insert(0, LedgerJsonContext.Default));

// ONE shared NpgsqlDataSource for BOTH data layers (EF Core + raw ADO.NET).
// A bare UseNpgsql(connString) would open a SECOND, connstring-keyed pool next
// to the data-source pool: two pools per process (Go runs exactly one pgxpool)
// and split db.client.connection.* metrics. Single registration, single pool.
var connString = NpgsqlConnectionString.Build(cfg);
var dataSource = new NpgsqlDataSourceBuilder(connString).Build();
builder.Services.AddSingleton(dataSource);

// EF Core pooled context + compiled model (committed; wired behind a flag if generation failed).
builder.Services.AddDbContextPool<LedgerDbContext>(o =>
{
   o.UseNpgsql(dataSource);
   CompiledModelWiring.Apply(o);
});

builder.Services.AddSingleton<IConnectionMultiplexer>(_ =>
{
   var options = ConfigurationOptions.Parse(cfg.RedisAddr);
   options.AbortOnConnectFail = false;
   return ConnectionMultiplexer.Connect(options);
});
builder.Services.AddSingleton(sp => new InvoiceCache(sp.GetRequiredService<IConnectionMultiplexer>(), cfg.CacheTtl));

// DATA_LAYER=efcore|ado|dapper DI switch (efcore is the headline; the other two are
// measured variants isolating the ORM component of the cost).
builder.Services.AddScoped<IInvoiceRepository>(sp => cfg.DataLayer switch
{
   DataLayer.Ado => new AdoInvoiceRepository(sp.GetRequiredService<NpgsqlDataSource>()),
   DataLayer.Dapper => new DapperInvoiceRepository(sp.GetRequiredService<NpgsqlDataSource>()),
   _ => new EfInvoiceRepository(sp.GetRequiredService<LedgerDbContext>()),
});

builder.Services.AddSingleton<PricingEngine>();
builder.Services.AddSingleton<RuntimeStatsCollector>();

builder.Services.AddValidation();
builder.Services.AddProblemDetails(ProblemDetailsConfig.Configure);
builder.Services.AddExceptionHandler<ProblemExceptionHandler>();

// Malformed-body handling must throw (BadHttpRequestException/JsonException ->
// ProblemExceptionHandler -> the frozen 400 {"body":["is required"]} + request_error log).
// ThrowOnBadRequest defaults to true only in Development; without this pin the Production
// container would emit a bare framework 400 with none of the frozen contract.
builder.Services.Configure<RouteHandlerOptions>(o => o.ThrowOnBadRequest = true);

var app = builder.Build();

app.Services.GetRequiredService<RuntimeStatsCollector>().Start();

// PDF_DIR created once at startup (parity with Go's MkdirAll; a directory that
// disappears mid-run is a 500 on both sides, not a silent re-create).
Directory.CreateDirectory(cfg.PdfDir);

// Warm the shared pool to min=max BEFORE serving traffic (parity with Go's
// db.WarmPool; spec/db-access.md min=max policy; the warmup gate asserts
// db_pool.total == max). Npgsql has no background min-filler: MinPoolSize only
// floors pruning, it never pre-opens. Open all DbPoolMax connections
// concurrently once, then return them to the pool as idle.
if (cfg.DbPoolWarmOnStart)
{
   var warm = new NpgsqlConnection[cfg.DbPoolMax];
   await Task.WhenAll(Enumerable.Range(0, cfg.DbPoolMax).Select(async i =>
   {
      warm[i] = await dataSource.OpenConnectionAsync();
   }));
   foreach (var c in warm)
      await c.DisposeAsync();
}

app.UseMiddleware<RequestIdMiddleware>();
app.UseMiddleware<AccessLogMiddleware>();
// The content-type gate sits inside the access-log wrapper (its 415s still get an
// access line) and before the exception/status-code writers.
app.UseMiddleware<MediaTypeGateMiddleware>();
app.UseExceptionHandler();
app.UseStatusCodePages();

app.MapInvoiceEndpoints();
app.MapCustomerEndpoints();
app.MapPricingEndpoints();
app.MapHealthEndpoints();
app.MapRuntimeStatsEndpoints();

// Effective-config line (manifest-captured), once at startup.
EffectiveConfig.LogOnce(app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Ledgerline"), cfg);

// Graceful shutdown: ApplicationStopped fires AFTER in-flight requests drain (parity with
// Go, which logs shutdown_complete after httpServer.Shutdown returns); the console queue
// is flushed when the provider disposes, after this callback.
var lifetime = app.Services.GetRequiredService<IHostApplicationLifetime>();
lifetime.ApplicationStopped.Register(() =>
{
   app.Services.GetRequiredService<RuntimeStatsCollector>().Dispose();
   LedgerLog.ShutdownComplete(app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Ledgerline"));
});

app.Run();

// Exposed for WebApplicationFactory-based tests.
public partial class Program;
