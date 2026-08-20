using Ledgerline.Config;
using Npgsql;

namespace Ledgerline.Infrastructure;

public static class NpgsqlConnectionString
{
   public static string Build(LedgerConfig cfg)
   {
      var b = new NpgsqlConnectionStringBuilder
      {
         Host = cfg.PgHost,
         Port = cfg.PgPort,
         Database = cfg.PgDb,
         Username = cfg.PgUser,
         Password = cfg.PgPassword,
         Pooling = true,
         MaxPoolSize = cfg.DbPoolMax,                 // DB_POOL_MAX (== Go MaxConns)
         MinPoolSize = cfg.DbPoolMin,                 // DB_POOL_MIN (= DB_POOL_MAX): no cold-start/idle-prune
         ConnectionLifetime = cfg.DbConnLifetimeSeconds, // explicit pin (DB_CONN_LIFETIME)
         MaxAutoPrepare = cfg.MaxAutoPrepare,         // 0 headline; 20 prepared-parity variant
         // Multiplexing left OFF (default): experimental, no pgx analog.
      };

      // AutoPrepareMinUsages only meaningful when MaxAutoPrepare > 0.
      if (cfg.MaxAutoPrepare > 0)
         b.AutoPrepareMinUsages = cfg.AutoPrepareMinUsages;

      return b.ConnectionString;
   }
}
