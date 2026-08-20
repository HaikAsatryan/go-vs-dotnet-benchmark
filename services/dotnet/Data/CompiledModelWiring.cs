using Ledgerline.Data.CompiledModels;
using Microsoft.EntityFrameworkCore;

namespace Ledgerline.Data;

/// Wires the EF compiled model (Data/CompiledModels, generated via `dotnet ef dbcontext optimize`,
/// committed). EF 10 also auto-discovers it via the [DbContextModel] assembly attribute; the
/// explicit .UseModel here is the documented, deterministic wiring.
///
/// This is a non-default EF optimisation with no Go analogue, so it is declared as an asymmetry
/// in docs/FAIRNESS.md: it lowers .NET's model-build and first-query cost, which makes the
/// paper's headline conservative rather than flattering.
public static class CompiledModelWiring
{
   public static void Apply(DbContextOptionsBuilder builder) =>
      builder.UseModel(LedgerDbContextModel.Instance);
}
