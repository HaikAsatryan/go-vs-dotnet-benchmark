using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;

namespace Ledgerline.Data;

/// Design-time factory so `dotnet ef dbcontext optimize` can build the model without a live DB
/// or any env config. Not used at runtime (Program.cs wires the pooled context).
public sealed class DesignTimeDbContextFactory : IDesignTimeDbContextFactory<LedgerDbContext>
{
   public LedgerDbContext CreateDbContext(string[] args)
   {
      var options = new DbContextOptionsBuilder<LedgerDbContext>()
         .UseNpgsql("Host=localhost;Database=ledgerline;Username=ledger;Password=ledger")
         .Options;
      return new LedgerDbContext(options);
   }
}
