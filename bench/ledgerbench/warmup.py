"""Warmup controller (spec/slo.yaml `warmup`).

Drives the SUT to steady state before every measured probe (every probe is
warmed), polling GET /runtime-stats at `poll_hz` and checking the
gate set. Hard cap `hard_cap_seconds`; if not steady by then the probe is
flagged under-warmed and that fact is recorded (never silently proceeded past).

Gates (spec/slo.yaml warmup.gates + per_endpoint_min_calls):
  - per-endpoint min call counts met (low-share endpoints tiered up),
  - p99 / p999 flat across trailing sub-windows (no positive trend),
  - GC steady, SYMMETRIC across languages: both require cadence flat AND
    heap_committed flat (Go cycle cadence + heap_committed; .NET gen counts +
    heap_committed). Headline memory metric is steady-state anon RSS, so neither
    language may pass with a still-climbing heap,
  - pools warm (db_pool.total == max; redis ready),
  - hot_set_touched + major_faults_asserted_equal: DECLARED in spec/slo.yaml but
    NOT IMPLEMENTED here. They are emitted per probe as
    {"gate": "...", "status": "not_implemented"} (see IMPLEMENTED_GATES) so an
    unimplemented gate can never be read as a passed one, and they never
    contribute to the under-warmed decision (a gate we do not evaluate must not
    flag probes either way). spec/ is frozen, so the fix is here, not there.

This module owns the *gate predicates* (pure, tested) + the polling loop that
applies them. The pure predicates take stats snapshots so they unit-test with no
network. Latency flatness uses the runner's own per-second p99 series (from the
warmup vegeta stream); a small monotone-trend test lives here to avoid a scipy
dependency in the runner (full Mann-Kendall lives in the analysis stage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from .config import WarmupCfg
from .runtimestats import RuntimeStats, Snapshot
from .util import paths
from .util.clock import rfc3339_micros

# Map mix endpoint names -> runtime-stats route-template keys for the call gate.
_MIX_TO_ROUTE = {
    "get_invoice": "GET /invoices/{id}",
    "list_invoices": "GET /customers/{id}/invoices",
    "pricing_quote": "POST /pricing/quote",
    "create_invoice": "POST /invoices",
    "pdf_stub": "POST /invoices/{id}/pdf-stub",
    "healthz": "GET /healthz",
}

# spec/slo.yaml warmup.gates entries this module actually evaluates. Anything
# else the spec declares is emitted per probe with status "not_implemented".
IMPLEMENTED_GATES = frozenset({"p99_flat", "p999_flat", "gc_steady", "pools_warm"})

# Gate status values used in the per-probe warmup.json record.
STATUS_MEASURED = "measured"
STATUS_NOT_IMPLEMENTED = "not_implemented"

# Trailing window (in 1 Hz polls / per-second latency samples) the flatness
# gates test. LIMITATION, deliberate and documented at the point of use: the
# Mann-Kendall test needs enough points to reach one-sided significance at
# alpha=0.05: with only 5 samples EVERY pair must be concordant (a strictly
# monotone rise) before the gate can fail, so a 5-sample window is very nearly
# unfailable. 30 samples (the last ~30 s of a warmup that is hard-capped at
# 180 s and normally ENDS EARLIER, once the gates settle) is short enough to
# still describe "the end of warmup" and long enough for the test to have power.
# Series shorter than this are used whole: which is why the smoke profile
# (15 s cap at 1 Hz) is unaffected by the widening.
#
# OPERATOR NOTE: this window was 5 samples on the 2026-06-06 run. Widening it
# makes p99_flat/p999_flat/gc_steady materially HARDER to pass, so the next
# measured run should be expected to flag MORE probes under_warmed than that
# one did (most visibly the gc-generous Go cell, where GOGC=off cannot satisfy
# gc_steady at all). That is the gate working, not a regression.
FLATNESS_TRAILING_SAMPLES = 30


# --------------------------------------------------------------------------- #
# pure gate predicates
# --------------------------------------------------------------------------- #
def endpoint_calls_met(stats: RuntimeStats, min_calls: dict[str, int]) -> tuple[bool, dict[str, int]]:
    """All per-endpoint counters >= their configured minimum.

    Returns (ok, shortfall_by_route) where shortfall is min - actual for any
    route still short (positive only).
    """
    shortfall: dict[str, int] = {}
    for mix_name, minimum in min_calls.items():
        route = _MIX_TO_ROUTE.get(mix_name, mix_name)
        actual = stats.endpoint_total(route)
        if actual < minimum:
            shortfall[route] = minimum - actual
    return (not shortfall), shortfall


def pools_warm(stats: RuntimeStats) -> bool:
    """db_pool.total == max (min=max policy) and redis pool present.

    With min=max the pool must be fully opened during warmup (spec/db-access.md
    "db_pool.total == max"). Redis readiness is "total >= 1" (go-redis pool) or
    the multiplexer connected (.NET reports total>=1 via the mapped counter).
    """
    db_ok = stats.db_pool.max > 0 and stats.db_pool.total >= stats.db_pool.max
    redis_ok = stats.redis_pool.total >= 1 or stats.redis_pool.max == 0
    return db_ok and redis_ok


def no_positive_trend(series: list[float], *, alpha: float = 0.05) -> bool:
    """Mann-Kendall test: no statistically significant positive monotone trend.

    Uses the SAME tie-corrected, continuity-corrected MK implementation as the
    analysis-stage stationarity gate (analysis.mann_kendall; numpy is a core
    runner dep), so the warmup flatness gate and the fit stationarity gate share
    one statistic. Significance at `alpha` (one-sided, upward) is required to
    FAIL: a raw concordant-pair majority (S > 0) on a short noisy-but-flat
    window is expected ~half the time and must not flag a probe under-warmed.
    A series of < 3 points is treated as "flat" (not enough evidence to fail).
    """
    if len(series) < 3:
        return True
    from analysis.mann_kendall import mann_kendall

    return not mann_kendall([float(x) for x in series]).has_positive_trend(alpha)


def gc_steady_go(committed_series: list[int], gc_cadence: list[int]) -> bool:
    """Go GC-steady: heap_committed flat AND collection-count cadence flat.

    Symmetric to `gc_steady_dotnet` (both languages require cadence-flat AND
    heap-committed-flat). The headline memory metric is steady-state anon RSS, so
    a Go probe must NOT pass warmup with a still-climbing heap even if its GC
    cadence has settled. `gc.heap_committed_bytes` for Go is the /runtime-stats
    "total minus released" (/memory/classes/total_bytes - .../heap/released_bytes);
    `gc_cadence` is the per-poll delta of gc.collections.total across the trailing
    window. Steady = both series show no positive trend.
    """
    return no_positive_trend([float(x) for x in committed_series]) and no_positive_trend(
        [float(x) for x in gc_cadence]
    )


def gc_steady_dotnet(committed_series: list[int], gen_cadence: list[int]) -> bool:
    """.NET GC-steady: heap_committed flat + gen-count cadence flat (DATAS)."""
    return no_positive_trend([float(x) for x in committed_series]) and no_positive_trend(
        [float(x) for x in gen_cadence]
    )


# --------------------------------------------------------------------------- #
# result record
# --------------------------------------------------------------------------- #
class GateResult(BaseModel):
    name: str
    passed: bool
    status: str = STATUS_MEASURED
    at_seconds: float | None = None
    detail: dict = Field(default_factory=dict)

    @property
    def enforced(self) -> bool:
        """Only measured gates decide under-warmed; not_implemented never does."""
        return self.status == STATUS_MEASURED


def not_implemented_gate(name: str) -> GateResult:
    """Explicit record for a spec-declared gate this runner does not evaluate.

    passed=False on purpose: a reader scanning for `passed: true` must not find
    one here. `enforced` is False so it cannot flag the probe under-warmed.
    """
    return GateResult(
        name=name,
        passed=False,
        status=STATUS_NOT_IMPLEMENTED,
        detail={
            "note": (
                f"spec/slo.yaml declares warmup gate {name!r}; the runner does "
                "not implement it. Recorded as not_implemented so its absence "
                "is visible; it neither passes nor flags the probe."
            )
        },
    )


class WarmupResult(BaseModel):
    """Written to `warmup.json` in the cell artifacts."""

    language: str
    rate: int
    elapsed_seconds: float
    under_warmed: bool                     # True => hard cap hit before all gates
    gates: list[GateResult] = Field(default_factory=list)
    final_db_pool_total: int | None = None
    final_db_pool_max: int | None = None
    # memory.stat pgmajfault delta over the probe's MEASURED window, filled by
    # the runner (disclosure only; the spec's major_faults_asserted_equal gate
    # is recorded in `gates` as not_implemented, and this number does not stand
    # in for it). None when the counter was unreadable.
    major_faults: int | None = None
    finished_utc: str = Field(default_factory=rfc3339_micros)

    def save(self, path: Path) -> None:
        paths.write_text_atomic(path, self.model_dump_json(indent=2))


@dataclass
class WarmupState:
    """Mutable accumulator the polling loop updates each tick (testable)."""

    cfg: WarmupCfg
    language: str
    rate: int
    p99_series: list[float] = field(default_factory=list)
    p999_series: list[float] = field(default_factory=list)
    committed_series: list[int] = field(default_factory=list)
    gc_total_series: list[int] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)

    def observe(self, snap: Snapshot, *, p99_ms: float | None = None, p999_ms: float | None = None) -> None:
        self.snapshots.append(snap)
        self.committed_series.append(snap.stats.gc.heap_committed_bytes)
        self.gc_total_series.append(snap.stats.gc.collections.total())
        if p99_ms is not None:
            self.p99_series.append(p99_ms)
        if p999_ms is not None:
            self.p999_series.append(p999_ms)

    def _gc_cadence(self) -> list[int]:
        s = self.gc_total_series
        return [s[i] - s[i - 1] for i in range(1, len(s))]

    def _gen_cadence(self) -> list[int]:
        # Reuse total-collection cadence as the gen proxy for the lightweight
        # runner check; the analysis stage splits by generation.
        return self._gc_cadence()

    def evaluate(self, *, trailing: int = FLATNESS_TRAILING_SAMPLES) -> list[GateResult]:
        """Apply all gates against the current trailing window.

        `trailing` is the flatness window in samples. It defaults to
        FLATNESS_TRAILING_SAMPLES rather than the old 5: at 5 samples the
        Mann-Kendall test can only reach significance on a strictly monotone
        run, i.e. the p99_flat/p999_flat/gc_steady gates were nearly unfailable
        (see the constant's comment). Every gate spec/slo.yaml declares but this
        module does not implement is emitted with status "not_implemented".
        """
        latest = self.snapshots[-1].stats if self.snapshots else RuntimeStats()
        gates: list[GateResult] = []

        calls_ok, shortfall = endpoint_calls_met(latest, self.cfg.per_endpoint_min_calls)
        gates.append(GateResult(name="per_endpoint_min_calls", passed=calls_ok, detail={"shortfall": shortfall}))

        if self.cfg.gates.get("p99_flat", False):
            gates.append(GateResult(
                name="p99_flat", passed=no_positive_trend(self.p99_series[-trailing:]),
                detail={"samples": len(self.p99_series[-trailing:])},
            ))
        if self.cfg.gates.get("p999_flat", False):
            gates.append(GateResult(
                name="p999_flat", passed=no_positive_trend(self.p999_series[-trailing:]),
                detail={"samples": len(self.p999_series[-trailing:])},
            ))
        if self.cfg.gates.get("gc_steady", False):
            committed = self.committed_series[-trailing:]
            if self.language == "dotnet":
                ok = gc_steady_dotnet(committed, self._gen_cadence()[-trailing:])
            else:
                ok = gc_steady_go(committed, self._gc_cadence()[-trailing:])
            gates.append(GateResult(name="gc_steady", passed=ok,
                                    detail={"samples": len(committed)}))
        if self.cfg.gates.get("pools_warm", False):
            gates.append(GateResult(name="pools_warm", passed=pools_warm(latest)))
        gates.extend(self.unimplemented_gates())
        return gates

    def unimplemented_gates(self) -> list[GateResult]:
        """Declared-but-unimplemented spec gates, in spec order."""
        return [
            not_implemented_gate(name)
            for name, enabled in self.cfg.gates.items()
            if enabled and name not in IMPLEMENTED_GATES
        ]

    def all_pass(self, *, trailing: int = FLATNESS_TRAILING_SAMPLES) -> bool:
        """True iff every ENFORCED gate passed (not_implemented gates abstain)."""
        return all(g.passed for g in self.evaluate(trailing=trailing) if g.enforced)
