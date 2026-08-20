# bench/ NOTES: design decisions and cross-language rationale

Where `spec/` left a degree of freedom, the rule applied here was: pick the
option with the lowest cross-language divergence risk and record it. `spec/`
is the single source of truth.

## Project layout

One uv-managed project rooted at `bench/` with three top-level packages:
`ledgerbench` (runner), `validation` (equivalence/CO/no-drop/sink/conn-sweep
suites), `analysis` (stats/fits/report). One lockfile, one venv;
`testpaths = ["tests", "validation/tests", "analysis/tests"]`.

Python pinned to 3.13 via `.python-version` (uv-managed download): 3.13 has
the widest wheel coverage for the scientific stack (numpy/scipy/pandas). Core runner deps (httpx/pydantic/pyyaml/
typer/rich) are insensitive to the choice.

## PRNG: hand-rolled PCG64, NOT numpy, NOT random.Random

`spec/workload.yaml` freezes `rng.algorithm: PCG64` and requires a
deterministic stream given `target_generator_seed`. Decisions:

- the frozen algorithm must not drift with a numpy upgrade, so the generator
  does not use `numpy.random.PCG64`.
- `random.Random` is a Mersenne Twister and would neither satisfy the frozen
  algorithm name nor be reproducible against any PCG64 reference.
- Therefore `util/pcg64.py` implements the canonical
  `pcg_setseq_128_xsl_rr_64` with the published PCG constants (the same
  constants numpy's PCG64 uses).

Seeding is intentionally SIMPLE and documented (seed -> low 64 bits of the
128-bit state, published default-odd increment) rather than numpy's
SeedSequence expansion. Consequence: within-Python determinism is GUARANTEED
(same seed -> byte-identical targets file). Byte-for-byte identity with the
seeder's raw PRNG words is NOT claimed: the seeder uses ChaCha8 for a
different purpose (DB contents) and the load generator uses PCG64 for the
request stream; `spec/workload.yaml rng` explicitly distinguishes the two
seeds. The seed is echoed in the manifest. Lowest-divergence because the
cross-language "sameness" that actually matters (hot-set, cache-hit
construction) is achieved at the DISTRIBUTION level (see Zipf below), not by
matching PRNG internals.

## Body sizing vs the wire schema

`spec/workload.yaml payloads` asks for create ~2 KB (±512) and quote ~8 KB
(±1024). But `spec/openapi.yaml` (higher priority: the wire contract) gives:

- `CreateInvoiceItem` HAS a `description` field (minLength 1, maxLength 200),
  flagged `filler: true` in workload.yaml -> the size knob. **create_invoice**
  bodies use COMPACT JSON and pad each item's `description` to land inside
  [target-512, target+512]. Padding never exceeds maxLength 200.
- `QuoteItem` has NO description field (only sku/qty/unit_price_minor). With
  50 compact items you reach only ~3 KB, far below 8 KB ± 1 KB. Adding an
  extra field would be non-idiomatic (services "ignore unknown fields", but
  how each ignores could differ) and is a divergence risk.
  **Lowest-divergence resolution:** keep exactly 50 schema-faithful items and
  reach `target_bytes` with JSON WHITESPACE (indentation). Both services
  discard insignificant JSON whitespace identically on parse, so this
  introduces ZERO behavioral skew while satisfying the byte-size assertion.
  The generator searches indent levels for the closest fit to 8192.

Both paths are deterministic (sizing uses no extra RNG draws); the
±tolerance assertions live in `bench/validation`.

## Zipf: finite inverse-CDF over ranks 1..N, alpha = 1.0

Per `spec/workload.yaml` the generator MUST use an explicit finite-support
inverse-CDF Zipf over ranks 1..N (NOT `numpy.random.zipf`, whose `a=s+1`
parameterization is the documented trap). `zipf.py` precomputes the
cumulative harmonic CDF (Kahan-compensated for large N) and binary-searches
each draw. rank 1 == id 1 (hottest). This same construction is the single
source of hot-set identity across the load generator, the seeder warm-touch,
and the cache-hit construction (distribution-level identity).

## Round-trip ledger

`spec/db-access.md` describes the create-path round-trip structure. The
runner does not implement the create path (the services do); it only
generates the create body and later measures. No code here is contorted to
hit a particular count; the wire round-trip count is measured on the measured
run and reported.

## 400 validation errors-map (resolved contract)

The errors-map KEY format (e.g. `Items[2].Sku`) is defined by .NET's built-in
source-generated validation emission, and the Go service matches it
byte-for-byte (see `services/dotnet/NOTES-equivalence.md` for the captured
contract). The equivalence suite therefore:

- asserts the frozen ENVELOPE (type/title/status/detail) exactly against the
  golden for every 400,
- asserts Go's map equals .NET's map (cross-service deep-equal) AND that
  every message VALUE is drawn from the frozen `x-validation-messages` set.

The runner emits no validation keys of its own.

## Image digests

Exact image tags are digest-pinned in the Dockerfiles and compose files. The
runner's `compose.py` additionally captures the digest the running container
actually reports (`docker inspect`) into the manifest, so the recorded digest
is always the real one.

## No Docker in unit tests

All Docker-touching paths are guarded (`compose.docker_available()` etc.) and
the pytest suite never starts a daemon. Docker-needing tests are marked
`@pytest.mark.docker` and skipped when the daemon is absent. `uv run pytest`
passes with no Docker.

## Normalizer rules (the spec branches that matter)

`validation/normalize.py`. Object keys sorted recursively; arrays are
ORDER-SIGNIFICANT and never reordered (every wire array is explicitly
ordered: items id ASC, list id DESC, quote lines input order, errors[field]
frozen list); `request_id` stripped at any depth (never in a body per spec,
defensive). `created_at` has TWO modes: `compare` (seeded read:
deterministic across services on the same DB load per spec/db-access.md,
value kept + format-checked) and `placeholder` (fresh POST /invoices:
now()-generated, masked). Timestamp format is enforced as RFC3339 UTC with
EXACTLY 6 fractional digits + Z, so both the .NET 7-digit 'O' trap and the Go
trailing-zero-trim trap FAIL the suite. Money keys (`*_minor`, `balance`)
must be integers; a float OR a bool there raises (Python bool is an int
subclass, rejected explicitly).

## Goldens: computed by hand AND cross-checked by a third implementation

`validation/golden/` holds the pricing examples A/B/C in two forms:
engine-level (real items only, minItems relaxed) and wire-level (padded to
exactly 50 with the documented zero-contribution filler `GR-0001 qty1
price0`). The integers are the spec worked-example values, transcribed as
literals in `test_golden_self_consistency.py`. THREE independent derivations
must agree per example: (1) the spec hand integers, (2) the committed golden
files, (3) `validation/pricing_reference.py` recomputing from the algorithm.
The reference is a genuine third implementation of `bps_apply` and the tier
tables, used by NO service. Error envelopes are the frozen `x-error-strings`
(validation/not_found/internal); `validation_messages.json` holds the frozen
`x-validation-messages` VALUES.

## Equivalence suite is env-driven and skips cleanly

`LEDGER_GO_URL` / `LEDGER_DOTNET_URL` select the two SUTs. Unset -> every
live test skips with a clear reason (the suite is import-clean and green with
no services). Seeded-id assumptions use SMOKE scale (customers 1..1000,
invoices 1..5000; rank 1 == id 1). Fresh-create equivalence drops the
server-assigned `id`/item-ids and placeholders `created_at` before
cross-comparison (those three legitimately differ between two independent
processes; everything else must match). RuntimeStats is SHAPE-ONLY
(keys/types via `shapes.json`), never values, per spec/runtime-stats.md.

## Docker/CO/sink/conn-sweep harnesses: descoped at scope freeze

The no-drop, sink-microbench, CO (docker pause / toxiproxy / self-saturation
/ oha) and connection-sweep modules, plus `test_harness_unit.py`, which
unit-tested their plan/argv/predicate logic without a daemon, were deleted
in the scope freeze without ever running live. The measured-run
validation phase runs the equivalence suite only; CO safety rests on the
per-probe achieved-vs-offered evidence plus smoke's docker-pause stall check
(`docs/METHODOLOGY.md` carries the retirement).

## Analysis stats are in-repo (no sklearn / pymannkendall)

`pava.py` is ~30-line PAVA isotonic on numpy (the true constraint is only
monotonicity); `mann_kendall.py` implements S + tie-corrected variance +
continuity-corrected z + one/two-sided p directly; `knee.py` reads the SLO
crossing off the isotonic fit with a bootstrap-OVER-REPS percentile CI (valid
at n>=10); `paired.py` does the paired-difference CI (t when Shapiro-normal,
else bootstrap on the paired diffs) with the pre-registered ±5% RELATIVE
margin verdict (inside -> tie, outside -> direction + Cohen's d_z effect
size). All pure and typed. Sub-knee ladders report `crossed=False` + a lower
bound rather than fabricating a crossing.

## Markdown tables without `tabulate`

`report.py` renders GitHub markdown tables by hand (`_df_to_md`) so the
report needs no `tabulate` dep beyond pandas. The $/M figure is explicitly
illustrative: a disclosed formula (`cpu_ms_per_req * vcpu_hour_usd / 3.6`) +
pinned price + a ±band, never a vendor quote.
