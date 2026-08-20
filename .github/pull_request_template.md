<!-- CONTRIBUTING.md has the long form. This is the short version of the same bar. -->

## What this changes

<!-- One or two sentences. If it is a methodology challenge, say what number it moves. -->

## Checklist

- [ ] **Symmetric.** A change to one service's behaviour has the equivalent change on the
      other side, or an explanation in `docs/FAIRNESS.md` of why the asymmetry is correct.
- [ ] **`spec/` untouched**, or the PR says plainly that it invalidates the published run.
      `spec/` is hashed into every run's `config_hash`; changing it means the committed
      numbers no longer describe this tree.
- [ ] **Three suites green locally**: `cd bench && uv sync && uv run pytest`,
      `cd services/go && gofmt -l . && go vet ./... && go test ./...`,
      `cd services/dotnet && dotnet format --verify-no-changes && dotnet test -c Release Ledgerline.Tests`.
- [ ] **`report.md` not hand-edited.** If analysis output changed, it was regenerated with
      `make analyze RUN=<run_id>` and the diff is in the PR.
