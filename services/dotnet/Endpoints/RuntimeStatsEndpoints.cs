using Ledgerline.Observability;
using StackExchange.Redis;

namespace Ledgerline.Endpoints;

public static class RuntimeStatsEndpoints
{
   public static void MapRuntimeStatsEndpoints(this WebApplication app)
   {
      // Excluded from the workload mix and access log. Polled at 1 Hz by the runner.
      app.MapGet("/runtime-stats", (RuntimeStatsCollector stats, IConnectionMultiplexer mux) =>
      {
         var (total, idle, inUse) = RedisCounters(mux);
         var snapshot = stats.Snapshot(total, idle, inUse);
         var bytes = RuntimeStatsWriter.Write(snapshot);
         return Results.Bytes(bytes, "application/json; charset=utf-8");
      });
   }

   private static (int total, int idle, int inUse) RedisCounters(IConnectionMultiplexer mux)
   {
      // SE.Redis is a multiplexer (single connection model). Map its counters best-effort;
      // the multiplexer model vs go-redis pool asymmetry is documented (spec/cache.md).
      try
      {
         var counters = mux.GetCounters();
         var outstanding = (int)Math.Min(counters.TotalOutstanding, int.MaxValue);
         return (mux.IsConnected ? 1 : 0, 0, outstanding);
      }
      catch
      {
         return (0, 0, 0);
      }
   }
}
