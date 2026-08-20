"""Post-run analysis: knee fits + CIs, stationarity, paired verdicts -> report.

Reads a finished `results/<run_id>/ledger.json` and turns the persisted
per-probe ledger data into the publishable analysis artifacts the docs promise
(`make analyze RUN=<id>`):

  - results/<run_id>/report/fits.json: machine-readable per-cell knee estimates
    (pooled AND per-language), the CPU-ms/request-vs-rate curve, the per-language
    p99-vs-SLO readout at the operating rate, stationarity summaries, the paired
    go-vs-.NET verdict PER OFFERED RATE, and the resource roll-up.
  - results/<run_id>/report/report.md; the same, assembled as markdown tables.

The fit inputs come from the SAME code path the live runner used to bracket the
SLO crossing: `phases._rungs_by_rate` reads the pooled per-probe p99 (the SLO
ground truth) and excludes under-warmed probes. We reuse it verbatim rather than
re-deriving the rung shape, so the analysis fits exactly what the run measured.

Five honesty rules this module enforces, because the published numbers are only
as good as their provenance:

  1. UNDER-WARMED PROBES NEVER ENTER A ROLL-UP SILENTLY. Every aggregate (the
     resource cost, the CPU-vs-rate curve, the per-language p99 readout) applies
     the same exclusion the fit input applies, and reports the used/flagged
     counts next to the number. Where a language has NO warm window at all, the
     numbers are still emitted, but labelled posture evidence, not a gated
     measurement, and the reason is DERIVED from the cell's own knobs and the
     per-window gate records, never asserted (see `_posture_notes`).
  2. THE POOLED KNEE IS ONLY AS POOLED AS ITS SURVIVING PROBES. The runner
     brackets the crossing on p99 pooled over both languages; that pooled knee
     sets the confirm rates and therefore the operating rate. Under-warmed
     exclusion is language-asymmetric, so a "pooled" fit can end up carrying the
     probes of ONE language only, in which case it IS that language's knee and
     saying otherwise is false. The per-language warm-probe composition of the
     fitted rungs is computed (`_pooled_composition`), published in fits.json,
     and drives the wording of the note.
  3. A PAIRED VERDICT CARRIES ITS RATE. Confirm runs at more than one bracket
     rate; pooling those blocks into one unlabelled verdict averages two
     different operating points. Verdicts are computed and published per rate.
  4. ONE AGGREGATION RULE FOR CPU-ms/REQUEST, STATED IN THE REPORT. Every
     published CPU-ms/request (the roll-up and every rung of the CPU-vs-rate
     curve) is REQUEST-WEIGHTED: total cgroup CPU delta over the aggregated
     windows divided by total requests serviced in them. So the request count
     printed beside it really is its denominator. The equal-weight-per-window
     alternative is computed too and the report quantifies the difference,
     because the two languages' window sets do not share a stage composition
     (a 600 s soak window services ~5x the requests of a confirm window).
  5. AN SLO VERDICT STATES ITS RULE. "Meets the promise" is the MEAN of the
     per-window p99s against the threshold (a lenient rule for a tail promise),
     so the count of windows that individually met it, and the worst window, are
     published in the same row.

The scientific stack (numpy/scipy/pandas) is the optional `[analysis]` group, so
the analysis modules are imported lazily inside the functions that need them.
Sections the run does not support (e.g. DB-CPU attribution, which needs Linux
cgroup evidence absent from a Windows/smoke run) are reported as an explicit
"not available in this run" line, never fabricated.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import phases
from .analysis_bridge import estimate_knee_from_rungs, stationarity_fails
from .config import load_slo, load_workload
from .ledger import Ledger, State
from .util import paths
from .warmup import IMPLEMENTED_GATES, STATUS_MEASURED

# Stages whose per-second p99 series back the stationarity gate (the same two
# stages the runner records `p99_series` for: the ladder and the long soak).
_STATIONARITY_STAGES = ("ladder", "soak")

# Stages whose per-probe-window cgroup summaries back the resource roll-up.
_RESOURCE_STAGES = ("confirm", "soak")

_LANGS = ("go", "dotnet")


# --------------------------------------------------------------------------- #
# small shared helpers over the persisted per-probe maps
# --------------------------------------------------------------------------- #
def _split_key(key: str) -> tuple[int, int, str] | None:
    """`r<rate>.b<block>.<lang>` -> (rate, block, lang); None if unparseable."""
    try:
        rate_s, block_s, lang = key.split(".")
        return int(rate_s[1:]), int(block_s[1:]), lang
    except (ValueError, IndexError):
        return None


def _probe_key(rate: int, block: int, lang: str) -> str:
    return f"r{rate}.b{block}.{lang}"


def _is_flagged(stage_data: dict, key: str, *, default: bool = False) -> bool:
    """True if this probe is under-warmed or gate-excluded (never measurement-grade).

    The ledger is the authority; `default` covers a probe present on disk but
    absent from the ledger map (an out-of-band window), where the caller passes
    the per-window `<lang>.warmup.json` verdict instead.
    """
    under_warmed = stage_data.get("under_warmed", {})
    excluded = stage_data.get("excluded", {})
    flagged = under_warmed.get(key, default)
    return bool(flagged) or key in excluded


def _rungs_by_rate_for_lang(stage_rec, lang: str, *, warm_only: bool = True) -> dict[int, list[float]]:
    """{rate: [per-probe pooled p99 ms]} for ONE language.

    The pooled fit (`phases._rungs_by_rate`) mixes both languages inside a rung;
    this is the per-language view that makes a language's own crossing visible.
    `warm_only=False` keeps under-warmed probes and is used ONLY for the explicit
    posture-evidence path (a language with zero warm probes in the whole ladder).
    """
    data = stage_rec.data
    by_rate: dict[int, list[float]] = {}
    for key, p99 in data.get("p99_ms", {}).items():
        parsed = _split_key(key)
        if parsed is None or parsed[2] != lang:
            continue
        if warm_only and _is_flagged(data, key):
            continue
        by_rate.setdefault(parsed[0], []).append(float(p99))
    return by_rate


def _p99_by_rate_lang(stage_rec) -> dict[tuple[int, str], dict[str, list[float]]]:
    """{(rate, lang): {"warm": [...], "all": [...]}} of per-probe pooled p99."""
    data = stage_rec.data
    out: dict[tuple[int, str], dict[str, list[float]]] = {}
    for key, p99 in data.get("p99_ms", {}).items():
        parsed = _split_key(key)
        if parsed is None:
            continue
        rate, _block, lang = parsed
        slot = out.setdefault((rate, lang), {"warm": [], "all": []})
        slot["all"].append(float(p99))
        if not _is_flagged(data, key):
            slot["warm"].append(float(p99))
    return out


def _exclusions_by_rate_lang(stage_rec) -> dict[tuple[int, str], dict[str, int]]:
    """{(rate, lang): {reason_kind: n}}: why each dropped window was dropped.

    A window can fail the warm-up gates AND the CO-safety gates at once, so each
    dropped probe is attributed to exactly ONE reason, gate exclusion first:
    "under-warmed" is the weaker statement, and reporting it over a probe that
    also blew the error-rate gate is what let the blog say "failed my warm-up
    gates" about ten windows that were really past the wall. Counted this way
    the reasons sum to (total - used) for every row.
    """
    out: dict[tuple[int, str], dict[str, int]] = {}
    data = stage_rec.data
    excluded = data.get("excluded", {})
    for key, reason in excluded.items():
        parsed = _split_key(key)
        if parsed is None:
            continue
        rate, _block, lang = parsed
        kind = "error rate" if str(reason).startswith("error_rate") else "rate fidelity"
        slot = out.setdefault((rate, lang), {})
        slot[kind] = slot.get(kind, 0) + 1
    for key, flagged in data.get("under_warmed", {}).items():
        if not flagged or key in excluded:
            continue
        parsed = _split_key(key)
        if parsed is None:
            continue
        rate, _block, lang = parsed
        slot = out.setdefault((rate, lang), {})
        slot["under-warmed"] = slot.get("under-warmed", 0) + 1
    return out


def _exclusion_summary(counts: dict[str, int]) -> str:
    """"3 under-warmed, 6 error rate"; the reasons a rung's windows were dropped."""
    if not counts:
        return "-"
    order = {"under-warmed": 0, "error rate": 1, "rate fidelity": 2}
    return ", ".join(
        f"{n} {kind}" for kind, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))
    )


def _pooled_composition(stage_rec) -> dict:
    """Which languages actually survive into the POOLED fit's rungs.

    `phases._rungs_by_rate` pools go and .NET per-probe p99 into one list per
    rung AFTER under-warmed/gate exclusion, and that exclusion is
    language-asymmetric. So "pooled" describes the intent, not necessarily the
    input: if every probe of one language is flagged, the surviving rungs carry
    the other language only and the fit IS that language's knee.

    Returns per-language warm-probe counts across the fitted rungs plus the
    number of rungs each language reaches, so the note the report prints is
    derived from the composition instead of asserting a mix that may not exist.
    """
    data = stage_rec.data
    warm_probes = {lang: 0 for lang in _LANGS}
    rungs_with = {lang: set() for lang in _LANGS}
    rungs: set[int] = set()
    for key in data.get("p99_ms", {}):
        parsed = _split_key(key)
        if parsed is None:
            continue
        rate, _block, lang = parsed
        if _is_flagged(data, key):
            continue
        rungs.add(rate)
        if lang in warm_probes:
            warm_probes[lang] += 1
            rungs_with[lang].add(rate)
    return {
        "n_rungs": len(rungs),
        "warm_probes": warm_probes,
        "rungs_reached": {lang: len(rungs_with[lang]) for lang in _LANGS},
        "languages_present": sorted(lg for lg in _LANGS if warm_probes[lg]),
    }


def _cell_extra_env(cell_id: str) -> dict[str, str]:
    """The per-cell runtime knob overrides this cell was declared with.

    Read from the same `phases.CELLS` table the runner executed, so a claim
    about a cell's GC posture ("GOGC=off") is derived from the run's own cell
    definition rather than asserted for every cell alike.
    """
    for cell in phases.CELLS:
        if cell.cell_id == cell_id:
            return dict(cell.extra_env)
    return {}


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cv_pct(values: list[float]) -> float | None:
    """Coefficient of variation (%): None when it is undefined (n<2 or mean 0)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean = statistics.fmean(vals)
    if mean == 0:
        return None
    return 100.0 * statistics.stdev(vals) / mean


def _fmt(x: float | None, spec: str = ".3f", dash: str = "n/a") -> str:
    return dash if x is None else format(x, spec)


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class KneeFit:
    """One knee estimate + the provenance needed to read it honestly.

    `scope` is the crucial field: "pooled" is the runner's own fit, over rungs
    that pool whatever go and .NET probes survived under-warmed exclusion;
    "go"/"dotnet" are the per-language refits over the same ladder rungs.
    `composition` (pooled scope only) records how many warm probes each language
    actually contributed, because a "pooled" fit whose rungs carry one language
    only is that language's knee, not an ensemble knee.
    """

    crossed: bool
    scope: str = "pooled"
    knee_rps: float | None = None
    ci_low_rps: float | None = None
    ci_high_rps: float | None = None
    ci_valid: bool | None = None
    min_reps_in_bracket: int | None = None
    n_rungs: int = 0
    n_rungs_dropped: int = 0        # rungs with no measurement-grade probe in scope
    warm_only: bool = True          # False => fitted over under-warmed probes (posture)
    max_rung_rps: float | None = None
    max_rung_p99_ms: float | None = None
    warning: str | None = None
    note: str | None = None
    composition: dict | None = None  # pooled scope: warm probes per language

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "crossed": self.crossed,
            "knee_rps": self.knee_rps,
            "ci_low_rps": self.ci_low_rps,
            "ci_high_rps": self.ci_high_rps,
            "ci_valid": self.ci_valid,
            "min_reps_in_bracket": self.min_reps_in_bracket,
            "n_rungs": self.n_rungs,
            "n_rungs_dropped": self.n_rungs_dropped,
            "warm_only": self.warm_only,
            "max_rung_rps": self.max_rung_rps,
            "max_rung_p99_ms": self.max_rung_p99_ms,
            "warning": self.warning,
            "note": self.note,
            "composition": self.composition,
        }


@dataclass
class SloAtRate:
    """One language's own measured p99 at one offered rate, against the SLO.

    `meets_slo` applies ONE stated rule: the MEAN of the aggregated windows'
    p99 values against the threshold. That is lenient for a tail promise, so
    `n_windows_met_slo` (how many windows individually met it) and `max_p99_ms`
    (the worst one) travel with it and are printed in the same table row.
    """

    stage: str
    rate: int
    lang: str
    n_warm: int
    n_total: int
    n_used: int                      # windows behind the mean/worst/met counts
    n_windows_met_slo: int
    mean_p99_ms: float
    max_p99_ms: float
    meets_slo: bool                  # rule: mean of per-window p99 <= slo_ms
    posture_only: bool               # True => no warm window; flagged probes only
    dropped_by: dict[str, int] = field(default_factory=dict)  # reason -> n, sums to total-used

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "rate": self.rate,
            "lang": self.lang,
            "n_warm": self.n_warm,
            "n_total": self.n_total,
            "n_used": self.n_used,
            "n_windows_met_slo": self.n_windows_met_slo,
            "mean_p99_ms": self.mean_p99_ms,
            "max_p99_ms": self.max_p99_ms,
            "meets_slo": self.meets_slo,
            "meets_slo_rule": "mean of per-window p99 <= slo_ms",
            "posture_only": self.posture_only,
            "dropped_by": dict(self.dropped_by),
        }


@dataclass
class CpuRateRung:
    """CPU-ms/request for both languages at ONE ladder rung."""

    rate: int
    go_cpu_ms_per_req: float | None
    dotnet_cpu_ms_per_req: float | None
    ratio_dotnet_over_go: float | None
    go_n_warm: int
    go_n_total: int
    dotnet_n_warm: int
    dotnet_n_total: int
    go_posture_only: bool
    dotnet_posture_only: bool

    def to_dict(self) -> dict:
        return {
            "rate": self.rate,
            "go_cpu_ms_per_req": self.go_cpu_ms_per_req,
            "dotnet_cpu_ms_per_req": self.dotnet_cpu_ms_per_req,
            "ratio_dotnet_over_go": self.ratio_dotnet_over_go,
            "go_n_warm": self.go_n_warm,
            "go_n_total": self.go_n_total,
            "dotnet_n_warm": self.dotnet_n_warm,
            "dotnet_n_total": self.dotnet_n_total,
            "go_posture_only": self.go_posture_only,
            "dotnet_posture_only": self.dotnet_posture_only,
        }


@dataclass
class CpuVsRate:
    """CPU-ms/request as a function of offered rate, per language.

    The headline ratio is a single operating point; this is the curve behind it.
    If per-request cost were rate-independent (the assumption the "cost at a
    fixed latency promise" framing rests on), both columns would be flat and the
    CVs near zero. They are not, so the curve is published.
    """

    available: bool
    rungs: list[CpuRateRung] = field(default_factory=list)
    cv_go_pct: float | None = None
    cv_dotnet_pct: float | None = None
    ratio_min: float | None = None
    ratio_max: float | None = None
    n_posture_only_values: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "rungs": [r.to_dict() for r in self.rungs],
            "cv_go_pct": self.cv_go_pct,
            "cv_dotnet_pct": self.cv_dotnet_pct,
            "ratio_min_dotnet_over_go": self.ratio_min,
            "ratio_max_dotnet_over_go": self.ratio_max,
            "n_posture_only_values": self.n_posture_only_values,
            "note": self.note,
        }


@dataclass
class PairedVerdict:
    """A confirm-stage paired go-vs-.NET difference on pooled p99, AT ONE RATE."""

    metric: str
    rate: int | None
    n_blocks: int
    mean_go: float
    mean_dotnet: float
    mean_diff_pct: float
    ci_pct: float                    # confidence level of the interval (e.g. 95.0)
    ci_low_pct: float
    ci_high_pct: float
    margin_pct: float                # pre-registered equivalence margin (+-)
    verdict: str
    effect_size: float

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "rate_rps": self.rate,
            "n_blocks": self.n_blocks,
            "mean_go": self.mean_go,
            "mean_dotnet": self.mean_dotnet,
            "mean_diff_pct": self.mean_diff_pct,
            "ci_pct": self.ci_pct,
            "ci_low_pct": self.ci_low_pct,
            "ci_high_pct": self.ci_high_pct,
            "margin_pct": self.margin_pct,
            "verdict": self.verdict,
            "effect_size": self.effect_size,
        }


@dataclass
class LangResource:
    """Per-language steady-state SUT resource cost (cgroup v2 ground truth).

    `cpu_ms_per_req` is REQUEST-WEIGHTED (`cpu_ms_total / requests`), so
    `requests` is literally its denominator. `cpu_ms_per_req_unweighted` is the
    equal-weight-per-window alternative, kept because the two languages'
    aggregated windows need not share a stage composition (`stage_windows`) and
    a 600 s soak window services roughly 5x the requests of a confirm window;
    the report prints both ratios so the weighting choice is auditable rather
    than implicit.
    """

    n_windows: int                   # measurement-grade windows in the roll-up
    n_flagged_excluded: int          # windows dropped as under-warmed/gate-excluded
    posture_only: bool               # True => EVERY window was flagged; see note
    flagged_gate_counts: dict[str, int] = field(default_factory=dict)
    stage_windows: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    non_2xx: int = 0
    create_share_pct: float | None = None
    cpu_ms_total: float = float("nan")
    cpu_ms_per_req: float = float("nan")            # request-weighted
    cpu_ms_per_req_unweighted: float = float("nan")  # equal weight per window
    anon_p99_mb: float = float("nan")
    mem_peak_mb: float = float("nan")
    throttle_clean: bool = True
    oom_clean: bool = True
    db_pool_total: int | None = None
    db_pool_max: int | None = None

    def to_dict(self) -> dict:
        return {
            "n_windows": self.n_windows,
            "n_flagged_excluded": self.n_flagged_excluded,
            "posture_only": self.posture_only,
            "flagged_gate_counts": self.flagged_gate_counts,
            "stage_windows": self.stage_windows,
            "requests": self.requests,
            "non_2xx": self.non_2xx,
            "create_share_pct": self.create_share_pct,
            "cpu_ms_total": self.cpu_ms_total,
            "cpu_ms_per_req": self.cpu_ms_per_req,
            "cpu_ms_per_req_aggregation": "request-weighted: cpu_ms_total / requests",
            "cpu_ms_per_req_unweighted": self.cpu_ms_per_req_unweighted,
            "anon_p99_mb": self.anon_p99_mb,
            "mem_peak_mb": self.mem_peak_mb,
            "throttle_clean": self.throttle_clean,
            "oom_clean": self.oom_clean,
            "db_pool_total": self.db_pool_total,
            "db_pool_max": self.db_pool_max,
        }


@dataclass
class ResourceCost:
    """Go-vs-.NET resource cost at the operating rate(s), or unavailable.

    The cgroup deltas live per-probe-window under `cells/<cell>/<stage>/.../`,
    not in the ledger; this is the "stats that live elsewhere" the report needs.
    `available=False` (no cgroup summaries on disk; a Windows/smoke run, or a
    run whose cells tree was not retained) renders an honest not-captured line
    instead of a fabricated number.

    `operating_rates` are selected as `rate <= pooled knee`. That filter is the
    POOLED knee (see KneeFit.scope), so it is a pooled-p99 operating point, not
    a per-language one.
    """

    available: bool
    operating_rates: list[int] = field(default_factory=list)
    go: LangResource | None = None
    dotnet: LangResource | None = None
    cpu_ratio: float | None = None              # .NET : Go, request-weighted
    cpu_ratio_unweighted: float | None = None   # same ratio, equal weight per window
    anon_ratio: float | None = None
    peak_ratio: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "operating_rates": self.operating_rates,
            "go": self.go.to_dict() if self.go else None,
            "dotnet": self.dotnet.to_dict() if self.dotnet else None,
            "cpu_ratio_dotnet_over_go": self.cpu_ratio,
            "cpu_ratio_dotnet_over_go_unweighted": self.cpu_ratio_unweighted,
            "anon_ratio_dotnet_over_go": self.anon_ratio,
            "peak_ratio_dotnet_over_go": self.peak_ratio,
            "note": self.note,
        }


@dataclass
class CellAnalysis:
    cell_id: str
    knee: KneeFit                                    # POOLED (both languages)
    knee_by_lang: dict[str, KneeFit] = field(default_factory=dict)
    stationarity: dict = field(default_factory=dict)
    paired: PairedVerdict | None = None              # primary (lowest) confirm rate
    paired_by_rate: list[PairedVerdict] = field(default_factory=list)
    slo_at_rates: list[SloAtRate] = field(default_factory=list)
    cpu_vs_rate: CpuVsRate | None = None
    resource: ResourceCost | None = None

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "knee_pooled": self.knee.to_dict(),
            "knee_by_language": {k: v.to_dict() for k, v in self.knee_by_lang.items()},
            "stationarity": self.stationarity,
            "paired_primary": self.paired.to_dict() if self.paired else None,
            "paired_by_rate": [p.to_dict() for p in self.paired_by_rate],
            "slo_at_rates": [s.to_dict() for s in self.slo_at_rates],
            "cpu_vs_rate": self.cpu_vs_rate.to_dict() if self.cpu_vs_rate else None,
            "resource": self.resource.to_dict() if self.resource else None,
        }


@dataclass
class AnalyzeResult:
    run_id: str
    slo_ms: float
    cells: list[CellAnalysis]
    fits_path: Path
    report_path: Path

    @property
    def analyzable(self) -> bool:
        return bool(self.cells)


# --------------------------------------------------------------------------- #
# per-cell analysis
# --------------------------------------------------------------------------- #
def _lang_label(lang: str) -> str:
    return "Go" if lang == "go" else ".NET"


def _pooled_note(comp: dict) -> str:
    """The pooled fit's note, DERIVED from which languages survived exclusion.

    Three cases, and only the first is the "ensemble crossing" the pooled label
    suggests:
      - both languages contribute warm probes -> a genuine two-stack fit;
      - exactly one does -> the "pooled" fit is that language's knee, and the
        confirm rates this run used were therefore selected by a one-language
        fit. Saying "not either language's knee" here would be false;
      - neither does -> the fit ran over an empty warm set (should not happen;
        `phases._rungs_by_rate` raises on an emptied rung).
    """
    present = comp["languages_present"]
    counts = ", ".join(
        f"{comp['warm_probes'][lg]} warm {_lang_label(lg)} probe(s) over "
        f"{comp['rungs_reached'][lg]} rung(s)"
        for lg in _LANGS
    )
    if len(present) > 1:
        return (
            f"fitted on p99 POOLED over go and .NET probes ({counts} of "
            f"{comp['n_rungs']} fitted rungs); the rung composition is uneven, so "
            f"this is the ensemble's crossing, not either language's knee."
        )
    if len(present) == 1:
        only = _lang_label(present[0])
        return (
            f"nominally pooled, but after under-warmed exclusion the fitted rungs "
            f"carry {only} probes ONLY ({counts} of {comp['n_rungs']} fitted "
            f"rungs): this fit IS {only}'s knee, and it is what selected this "
            f"cell's confirm rates."
        )
    return (
        f"no warm probe of either language survived exclusion in the fitted "
        f"rungs ({counts}); the fit has no measurement-grade input."
    )


def _knee_for_cell(ladder_rec, slo_ms: float, confirm_reps: int) -> KneeFit:
    """Fit the POOLED knee from the ladder's persisted per-probe p99.

    Reuses `phases._rungs_by_rate` (the runner's fit-input code path) so the
    analysis fits exactly the rung shape the run bracketed against. A ladder with
    no SLO crossing, or fewer than 2 measured rungs, is reported as such rather
    than forced through a fit that would fabricate a crossing.

    The rungs pool go and .NET probes AFTER a language-asymmetric under-warmed
    exclusion, so what "pooled" means is a property of the data, not of the
    label: `_pooled_composition` counts the warm probes each language actually
    contributed and `_pooled_note` words the note accordingly. `_knee_for_lang`
    below is the per-language counterpart.
    """
    crossed = bool(ladder_rec.data.get("crossed", False))
    rungs_by_rate = phases._rungs_by_rate(ladder_rec)
    n_rungs = len(rungs_by_rate)
    comp = _pooled_composition(ladder_rec)

    if not crossed:
        return KneeFit(
            crossed=False, scope="pooled", n_rungs=n_rungs, composition=comp,
            note="no SLO crossing in the ladder range; no knee to fit "
                 "(sub-knee ladder, expected at smoke scale).",
        )
    if n_rungs < 2:
        return KneeFit(
            crossed=True, scope="pooled", n_rungs=n_rungs, composition=comp,
            note="ladder bracketed a crossing but fewer than 2 measured rungs "
                 "remain after under-warmed exclusion; cannot fit a knee.",
        )

    knee = estimate_knee_from_rungs(rungs_by_rate, slo_ms=slo_ms, min_reps=confirm_reps)
    return KneeFit(
        crossed=knee.crossed,
        scope="pooled",
        knee_rps=knee.knee_rps,
        ci_low_rps=knee.ci_low_rps,
        ci_high_rps=knee.ci_high_rps,
        ci_valid=knee.ci_valid,
        min_reps_in_bracket=knee.min_reps_in_bracket,
        n_rungs=n_rungs,
        composition=comp,
        note=_pooled_note(comp),
        warning=knee.warning,
    )


def _knee_for_lang(ladder_rec, lang: str, slo_ms: float, confirm_reps: int) -> KneeFit:
    """Refit the knee from the SAME ladder rungs, restricted to one language.

    Rungs with no measurement-grade probe for this language are DROPPED (counted
    in `n_rungs_dropped`) rather than back-filled. A language that never reaches
    the SLO inside the ladder range is reported as "no crossing", with the top
    rung's rate and mean p99 so a reader can see how far the ladder actually got
   ; we do not extrapolate past the highest rate the run offered.

    A language with ZERO warm probes across the whole ladder (in this run's
    gc-generous cell, Go: see the derived posture note in the report for the
    gates it failed) gets a POSTURE-ONLY refit over its flagged probes, marked
    `warm_only=False`.
    """
    all_rungs = _rungs_by_rate_for_lang(ladder_rec, lang, warm_only=False)
    warm_rungs = _rungs_by_rate_for_lang(ladder_rec, lang, warm_only=True)
    warm_only = bool(warm_rungs)
    rungs = warm_rungs if warm_only else all_rungs
    dropped = len(all_rungs) - len(rungs)
    posture_note = (
        None if warm_only else
        f"POSTURE ONLY: every ladder probe for {lang} was flagged under-warmed, "
        f"so this fit runs over flagged probes and is not a gated measurement."
    )

    if len(rungs) < 2:
        return KneeFit(
            crossed=False, scope=lang, n_rungs=len(rungs), n_rungs_dropped=dropped,
            warm_only=warm_only,
            note=posture_note or (
                f"fewer than 2 rungs carry a measurement-grade {lang} probe "
                f"({dropped} rung(s) dropped by under-warmed/gate exclusion); "
                f"cannot fit a per-language knee."
            ),
        )

    top_rate = max(rungs)
    top_p99 = statistics.fmean(rungs[top_rate])
    knee = estimate_knee_from_rungs(rungs, slo_ms=slo_ms, min_reps=confirm_reps)
    if not knee.crossed:
        return KneeFit(
            crossed=False, scope=lang, n_rungs=len(rungs), n_rungs_dropped=dropped,
            warm_only=warm_only, max_rung_rps=float(top_rate), max_rung_p99_ms=top_p99,
            note=(posture_note + " " if posture_note else "") +
                 f"no crossing within the measured ladder (max rung {top_rate} rps, "
                 f"p99 {top_p99:.2f} ms); not extrapolated.",
        )
    return KneeFit(
        crossed=True, scope=lang,
        knee_rps=knee.knee_rps,
        ci_low_rps=knee.ci_low_rps, ci_high_rps=knee.ci_high_rps,
        ci_valid=knee.ci_valid, min_reps_in_bracket=knee.min_reps_in_bracket,
        n_rungs=len(rungs), n_rungs_dropped=dropped, warm_only=warm_only,
        max_rung_rps=float(top_rate), max_rung_p99_ms=top_p99,
        note=posture_note,
        warning=knee.warning,
    )


def _slo_at_rates(cell_rec, slo_ms: float) -> list[SloAtRate]:
    """Each language's OWN p99 at each confirm/soak rate, against the SLO.

    The runner holds both stacks to the same OFFERED RATE, never to the same
    latency promise, so "the operating rate" can be sub-knee for one language and
    supra-knee for the other. This is the readout that makes that visible.

    THE VERDICT RULE, stated once here and again in the report: `meets_slo` is
    `mean(per-window p99) <= slo_ms`. It is deliberately the same rule the
    runner's own bracketing uses (a rung mean), but it is lenient for a tail
    promise; a language can be marked "meets" while individual windows miss by
    a wide margin, so the per-window met count and the worst window are carried
    alongside and printed next to the verdict, never instead of it.
    """
    rows: list[SloAtRate] = []
    for stage in _RESOURCE_STAGES:
        rec = getattr(cell_rec, stage)
        if rec.state != State.DONE:
            continue
        dropped = _exclusions_by_rate_lang(rec)
        for (rate, lang), vals in sorted(_p99_by_rate_lang(rec).items()):
            warm, allv = vals["warm"], vals["all"]
            used = warm or allv
            if not used:
                continue
            rows.append(SloAtRate(
                stage=stage, rate=rate, lang=lang,
                n_warm=len(warm), n_total=len(allv),
                n_used=len(used),
                n_windows_met_slo=sum(1 for p in used if p <= slo_ms),
                mean_p99_ms=statistics.fmean(used), max_p99_ms=max(used),
                meets_slo=statistics.fmean(used) <= slo_ms,
                posture_only=not warm,
                dropped_by=dropped.get((rate, lang), {}),
            ))
    return rows


def _stationarity_for_cell(cell_rec, slo_ms_cfg) -> dict:
    """Per-probe Mann-Kendall stationarity over each persisted per-second series.

    Walks the stages that record `p99_series` (ladder, soak). A probe with a
    series of >=3 points is gated; the summary records per-probe fails and an
    `any_fail` rollup so a non-stationary probe is visible without re-deriving it.
    """
    min_slope = 1.0
    if slo_ms_cfg and "min_slope_ms_per_min" in slo_ms_cfg:
        min_slope = float(slo_ms_cfg["min_slope_ms_per_min"])

    per_probe: dict[str, bool] = {}
    n_series = 0
    for stage in _STATIONARITY_STAGES:
        series_by_key = getattr(cell_rec, stage).data.get("p99_series", {})
        for key, vals in series_by_key.items():
            if len(vals) < 3:
                continue
            n_series += 1
            per_probe[f"{stage}/{key}"] = stationarity_fails(
                vals, min_slope_ms_per_min=min_slope
            )
    return {
        "n_series": n_series,
        "any_fail": any(per_probe.values()),
        "per_probe_fails": per_probe,
        "min_slope_ms_per_min": min_slope,
        "available": n_series > 0,
    }


def _paired_p99_by_block(confirm_rec) -> dict[int, tuple[list[float], list[float]]]:
    """{rate: (go p99 list, dotnet p99 list)} aligned by confirm block.

    Reads the confirm stage's persisted per-probe pooled p99
    (`p99_ms[r<rate>.b<block>.<lang>]`) and pairs go with .NET BY BLOCK (the RCB
    pairing key). Under-warmed probes are excluded the same way the fit input is.
    Only blocks with BOTH languages present and warm contribute a pair.

    Blocks are grouped BY OFFERED RATE and never merged across rates: confirm
    runs at both bracket rates, and one unlabelled verdict over the union would
    average two different operating points.
    """
    data = confirm_rec.data
    by_rate_block: dict[int, dict[int, dict[str, float]]] = {}
    for key, p99 in data.get("p99_ms", {}).items():
        parsed = _split_key(key)
        if parsed is None or _is_flagged(data, key):
            continue
        rate, block, lang = parsed
        by_rate_block.setdefault(rate, {}).setdefault(block, {})[lang] = float(p99)

    out: dict[int, tuple[list[float], list[float]]] = {}
    for rate in sorted(by_rate_block):
        go: list[float] = []
        dotnet: list[float] = []
        for block in sorted(by_rate_block[rate]):
            pair = by_rate_block[rate][block]
            if "go" in pair and "dotnet" in pair:
                go.append(pair["go"])
                dotnet.append(pair["dotnet"])
        out[rate] = (go, dotnet)
    return out


def _paired_for_cell(confirm_rec, margin_pct: float) -> list[PairedVerdict]:
    """Paired go-vs-.NET p99 verdicts, ONE PER OFFERED CONFIRM RATE.

    Empty when the confirm stage has no completed paired blocks (e.g. a run that
    stopped before confirm, or a cell where one language has no warm window). p99
    is lower-is-better, so the direction word reads "go better" when Go's p99 is
    the lower one.
    """
    if confirm_rec.state != State.DONE:
        return []

    from analysis.paired import paired_difference

    ci_level = 0.95
    out: list[PairedVerdict] = []
    for rate, (go, dotnet) in _paired_p99_by_block(confirm_rec).items():
        if len(go) < 2:
            continue
        r = paired_difference(
            go, dotnet, metric="p99_ms", margin_pct=margin_pct,
            lower_is_better=True, ci=ci_level,
        )
        out.append(PairedVerdict(
            metric=r.metric, rate=rate, n_blocks=r.n_blocks,
            mean_go=r.mean_go, mean_dotnet=r.mean_dotnet,
            mean_diff_pct=r.mean_diff_pct,
            ci_pct=r.ci_pct,
            ci_low_pct=r.ci_low_pct, ci_high_pct=r.ci_high_pct,
            margin_pct=r.margin_pct, verdict=r.verdict, effect_size=r.effect_size,
        ))
    return out


def _failed_gate_names(warmup: dict | None) -> list[str]:
    """Names of the warmup gates a flagged window actually FAILED.

    A declared-but-unimplemented gate is recorded with `passed: false` on
    purpose (`warmup.not_implemented_gate`, so nobody can read it as a pass) but
    `status: not_implemented`, and it never flags a probe. Counting it as a
    failure here would attribute the flagging to a gate that was never
    evaluated, so only `status == measured` records count. Records written
    before the status field existed default to measured.
    """
    if not warmup:
        return []
    return [
        g.get("name", "?")
        for g in warmup.get("gates", [])
        if not g.get("passed", True) and g.get("status", STATUS_MEASURED) == STATUS_MEASURED
    ]


def _window_rows(
    run_id: str, cell_rec, stages: tuple[str, ...], rate_filter=None
) -> dict[str, dict[str, list[dict]]]:
    """Read every per-probe window on disk for a cell -> {lang: {"warm"|"flagged": [rows]}}.

    A row carries what both the resource roll-up and the CPU-vs-rate curve need:
    per-request CPU, memory, throttle/OOM counters, serviced requests, status
    codes, the warmed pool, and the failed warmup gates for a flagged window.
    The ledger's under-warmed/excluded maps decide warm vs flagged; a window
    absent from the ledger falls back to its own `<lang>.warmup.json` verdict.
    """
    base = paths.run_dir(run_id) / "cells" / cell_rec.cell_id
    out: dict[str, dict[str, list[dict]]] = {
        lang: {"warm": [], "flagged": []} for lang in _LANGS
    }
    for stage in stages:
        stage_data = getattr(cell_rec, stage).data
        sdir = base / stage
        if not sdir.is_dir():
            continue
        for ratedir in sorted(sdir.glob("rate*")):
            try:
                rate = int(ratedir.name[len("rate"):])
            except ValueError:
                continue
            if rate_filter is not None and not rate_filter(rate):
                continue
            for blockdir in sorted(ratedir.glob("block*")):
                try:
                    block = int(blockdir.name[len("block"):])
                except ValueError:
                    continue
                for lang in _LANGS:
                    cm = _read_json(blockdir / f"cpu_mem_{lang}.summary.json")
                    lat = _read_json(blockdir / f"{lang}.summary.json")
                    if not cm or not lat:
                        continue
                    reqs = int(lat.get("requests", 0) or 0)
                    cpu_us = cm.get("cpu_usage_delta_usec")
                    if reqs <= 0 or cpu_us is None:
                        continue
                    wu = _read_json(blockdir / f"{lang}.warmup.json")
                    key = _probe_key(rate, block, lang)
                    flagged = _is_flagged(
                        stage_data, key,
                        default=bool(wu.get("under_warmed")) if wu else False,
                    )
                    codes = {str(k): int(v) for k, v in (lat.get("status_codes") or {}).items()}
                    row = {
                        "stage": stage,
                        "rate": rate,
                        "block": block,
                        "cpu_ms": cpu_us / 1000.0,
                        "cpu_ms_per_req": (cpu_us / 1000.0) / reqs,
                        "anon_p99": cm.get("mem_anon_p99"),
                        "mem_peak": cm.get("mem_peak"),
                        "requests": reqs,
                        "status_codes": codes,
                        "throttled": int(cm.get("nr_throttled_end", 0) or 0),
                        "oom": int(cm.get("oom_events", 0) or 0),
                        "db_pool_total": (wu or {}).get("final_db_pool_total"),
                        "db_pool_max": (wu or {}).get("final_db_pool_max"),
                        "failed_gates": _failed_gate_names(wu),
                    }
                    out[lang]["flagged" if flagged else "warm"].append(row)
    return out


def _cpu_ms_per_req(rows: list[dict]) -> float:
    """THE aggregation rule: total cgroup CPU-ms / total serviced requests.

    Request-weighted, so the request count published beside the figure really is
    its denominator. Used for every CPU-ms/request in this module (the rung
    values and the roll-up alike) so the report has one rule to state.
    """
    reqs = sum(r["requests"] for r in rows)
    return sum(r["cpu_ms"] for r in rows) / reqs if reqs else float("nan")


def _cpu_vs_rate_for_cell(run_id: str, cell_rec) -> CpuVsRate:
    """CPU-ms/request per ladder rung per language + the ratio and CVs.

    Measurement-grade windows only; where a rung has NO warm window for a
    language, the flagged windows at that rung are used instead and the value is
    marked posture-only (rather than leaving a hole that hides the rung). The CVs
    are taken across the published rung values so the rate dependence is a
    number, not an impression.

    Values use the module's one aggregation rule (`_cpu_ms_per_req`): total
    cgroup CPU delta over the rung's windows divided by the requests they
    serviced.
    """
    rows = _window_rows(run_id, cell_rec, ("ladder",))
    if not any(rows[lang][kind] for lang in _LANGS for kind in ("warm", "flagged")):
        return CpuVsRate(
            available=False,
            note="no per-window cgroup summaries under cells/<cell>/ladder for "
                 "either language (Windows/smoke run, or a run whose cells tree "
                 "was not retained).",
        )

    by_rate: dict[int, dict[str, dict[str, list[dict]]]] = {}
    for lang in _LANGS:
        for kind in ("warm", "flagged"):
            for r in rows[lang][kind]:
                slot = by_rate.setdefault(r["rate"], {})
                slot.setdefault(lang, {"warm": [], "flagged": []})[kind].append(r)

    rungs: list[CpuRateRung] = []
    posture_values = 0
    for rate in sorted(by_rate):
        vals: dict[str, float | None] = {}
        meta: dict[str, tuple[int, int, bool]] = {}
        for lang in _LANGS:
            slot = by_rate[rate].get(lang, {"warm": [], "flagged": []})
            warm, flagged = slot["warm"], slot["flagged"]
            used = warm or flagged
            posture = not warm and bool(flagged)
            posture_values += 1 if posture else 0
            vals[lang] = _cpu_ms_per_req(used) if used else None
            meta[lang] = (len(warm), len(warm) + len(flagged), posture)
        g, d = vals["go"], vals["dotnet"]
        rungs.append(CpuRateRung(
            rate=rate,
            go_cpu_ms_per_req=g, dotnet_cpu_ms_per_req=d,
            ratio_dotnet_over_go=(d / g) if (g and d) else None,
            go_n_warm=meta["go"][0], go_n_total=meta["go"][1],
            dotnet_n_warm=meta["dotnet"][0], dotnet_n_total=meta["dotnet"][1],
            go_posture_only=meta["go"][2], dotnet_posture_only=meta["dotnet"][2],
        ))

    ratios = [r.ratio_dotnet_over_go for r in rungs if r.ratio_dotnet_over_go is not None]
    return CpuVsRate(
        available=True,
        rungs=rungs,
        cv_go_pct=_cv_pct([r.go_cpu_ms_per_req for r in rungs]),
        cv_dotnet_pct=_cv_pct([r.dotnet_cpu_ms_per_req for r in rungs]),
        ratio_min=min(ratios) if ratios else None,
        ratio_max=max(ratios) if ratios else None,
        n_posture_only_values=posture_values,
        note="per rung: total cgroup CPU delta / total serviced requests over "
             "that rung's probe windows "
             "(cells/<cell>/ladder/rate*/block*/cpu_mem_<lang>.summary.json).",
    )


def _roll_windows(rows: list[dict], flagged_rows: list[dict]) -> LangResource:
    """Aggregate one language's measurement-grade windows into a LangResource.

    CPU-ms/request is request-weighted (`_cpu_ms_per_req`); the equal-weight
    alternative and the confirm/soak window composition are recorded alongside
    so the report can quantify what the weighting choice is worth instead of
    leaving an unlabelled mean of ratios in the headline.
    """
    n = len(rows)
    stage_windows: dict[str, int] = {}
    for r in rows:
        stage_windows[r["stage"]] = stage_windows.get(r["stage"], 0) + 1
    anon = [r["anon_p99"] for r in rows if r["anon_p99"] is not None]
    peak = [r["mem_peak"] for r in rows if r["mem_peak"] is not None]
    requests = sum(r["requests"] for r in rows)
    codes: dict[str, int] = {}
    for r in rows:
        for code, cnt in r["status_codes"].items():
            codes[code] = codes.get(code, 0) + cnt
    non_2xx = sum(c for code, c in codes.items() if not code.startswith("2"))
    created = codes.get("201", 0)
    gate_counts: dict[str, int] = {}
    for r in flagged_rows:
        for name in r["failed_gates"]:
            gate_counts[name] = gate_counts.get(name, 0) + 1
    pools = [r["db_pool_total"] for r in rows if r["db_pool_total"] is not None]
    pool_max = [r["db_pool_max"] for r in rows if r["db_pool_max"] is not None]
    return LangResource(
        n_windows=n,
        n_flagged_excluded=len(flagged_rows),
        posture_only=False,
        flagged_gate_counts=gate_counts,
        stage_windows=stage_windows,
        requests=requests,
        non_2xx=non_2xx,
        create_share_pct=(100.0 * created / requests) if requests else None,
        cpu_ms_total=sum(r["cpu_ms"] for r in rows),
        cpu_ms_per_req=_cpu_ms_per_req(rows),
        cpu_ms_per_req_unweighted=statistics.fmean(r["cpu_ms_per_req"] for r in rows),
        anon_p99_mb=(sum(anon) / len(anon) / 1e6) if anon else float("nan"),
        mem_peak_mb=(sum(peak) / len(peak) / 1e6) if peak else float("nan"),
        throttle_clean=all(r["throttled"] == 0 for r in rows),
        oom_clean=all(r["oom"] == 0 for r in rows),
        db_pool_total=max(pools) if pools else None,
        db_pool_max=max(pool_max) if pool_max else None,
    )


def _resource_cost_for_cell(run_id: str, cell_rec, knee_rps: float | None) -> ResourceCost:
    """Steady-state SUT cgroup CPU + anon memory per request, Go vs .NET.

    Walks the persisted per-probe-window cgroup summaries the live runner wrote
    under `cells/<cell>/{confirm,soak}/rate<R>/block<B>/cpu_mem_<lang>.summary.json`
    (CPU usec delta + anon-p99 + memory.peak + throttle/oom) alongside the latency
    summary (`<lang>.summary.json`, for the serviced-request count and status
    codes) and the warmup record (`<lang>.warmup.json`, for the warmed pool size
    and the gates a flagged window failed).

    UNDER-WARMED / GATE-EXCLUDED WINDOWS ARE EXCLUDED, exactly as the knee fit
    input excludes them, and both counts (used vs excluded) travel with the
    numbers. A language whose windows are ALL flagged is still reported
    (dropping it would hide a whole cell's posture), but it is marked
    `posture_only=True`, and `_posture_notes` prints the gates it failed plus
    whatever the cell's OWN declared knobs explain about them.

    Only rates at or below the (POOLED) knee are aggregated, because per-request
    cost at a supra-knee rung reports saturation behaviour. With no fitted knee,
    all confirm/soak rates are used. Returns `available=False` when no cgroup
    summaries exist on disk (Windows/smoke run, or a non-retained cells tree).
    """
    rows = _window_rows(
        run_id, cell_rec, _RESOURCE_STAGES,
        rate_filter=None if knee_rps is None else (lambda rate: rate <= knee_rps),
    )
    have_any = any(rows[lang][kind] for lang in _LANGS for kind in ("warm", "flagged"))
    if not have_any:
        return ResourceCost(
            available=False,
            note="no cgroup-v2 SUT resource summaries on disk for this cell "
                 "(Windows/smoke run, or a run whose per-window cells tree was "
                 "not retained); per-request CPU/memory cannot be reported.",
        )

    langs: dict[str, LangResource] = {}
    rates: set[int] = set()
    for lang in _LANGS:
        warm, flagged = rows[lang]["warm"], rows[lang]["flagged"]
        used = warm or flagged
        if not used:
            return ResourceCost(
                available=False,
                note=f"no usable {lang} resource windows for this cell; a "
                     f"Go-vs-.NET ratio needs both languages.",
            )
        langs[lang] = _roll_windows(used, flagged)
        if not warm:
            langs[lang].posture_only = True
            langs[lang].n_flagged_excluded = 0   # nothing was excluded; all were used
        rates.update(r["rate"] for r in used)

    go, dn = langs["go"], langs["dotnet"]
    return ResourceCost(
        available=True,
        operating_rates=sorted(rates),
        go=go, dotnet=dn,
        cpu_ratio=dn.cpu_ms_per_req / go.cpu_ms_per_req if go.cpu_ms_per_req else None,
        cpu_ratio_unweighted=(
            dn.cpu_ms_per_req_unweighted / go.cpu_ms_per_req_unweighted
            if go.cpu_ms_per_req_unweighted else None
        ),
        anon_ratio=dn.anon_p99_mb / go.anon_p99_mb if go.anon_p99_mb else None,
        peak_ratio=dn.mem_peak_mb / go.mem_peak_mb if go.mem_peak_mb else None,
    )


def _analyze_cell(cell_rec, slo, run_id: str) -> CellAnalysis | None:
    """One cell: knees + stationarity + paired + SLO readout + resource curves."""
    ladder_rec = cell_rec.ladder
    if ladder_rec.state != State.DONE:
        return None
    slo_ms = float(slo.slo.threshold_ms)
    knee = _knee_for_cell(ladder_rec, slo_ms, slo.ladder.confirm_reps)
    by_lang = {
        lang: _knee_for_lang(ladder_rec, lang, slo_ms, slo.ladder.confirm_reps)
        for lang in _LANGS
    }
    stat = _stationarity_for_cell(cell_rec, slo.stationarity)
    paired_by_rate = _paired_for_cell(cell_rec.confirm, slo.equivalence.margin_pct)
    resource = _resource_cost_for_cell(run_id, cell_rec, knee.knee_rps)
    return CellAnalysis(
        cell_id=cell_rec.cell_id,
        knee=knee, knee_by_lang=by_lang, stationarity=stat,
        paired=paired_by_rate[0] if paired_by_rate else None,
        paired_by_rate=paired_by_rate,
        slo_at_rates=_slo_at_rates(cell_rec, slo_ms),
        cpu_vs_rate=_cpu_vs_rate_for_cell(run_id, cell_rec),
        resource=resource,
    )


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def _md(rows: list[dict]) -> str:
    from analysis.report import _df_to_md
    import pandas as pd
    return _df_to_md(pd.DataFrame(rows))


def _knee_row(cell_id: str, k: KneeFit, slo_ms: float) -> dict:
    if not k.crossed:
        knee_s, ci_s, valid_s = "no crossing", "n/a", "n/a"
    elif k.knee_rps is None:
        knee_s, ci_s, valid_s = "unfit", "n/a", "n/a"
    else:
        knee_s = f"{k.knee_rps:.0f}"
        ci_s = f"[{k.ci_low_rps:.0f}, {k.ci_high_rps:.0f}]"
        valid_s = "yes" if k.ci_valid else "NOT a CI (see note)"
    if k.scope == "pooled":
        # Label the pooled row by what it actually contains: a "pooled" fit whose
        # rungs kept one language's probes only is that language's knee.
        present = (k.composition or {}).get("languages_present", list(_LANGS))
        scope = (
            f"pooled -> {_lang_label(present[0])} probes only"
            if len(present) == 1 else "pooled (go+.NET)"
        )
    else:
        scope = k.scope
    if not k.warm_only:
        scope += " *posture*"
    # The knee module's warning ends with "...the >=10-rep confirm stage produces
    # the inferential CI", which this run never did; keep only its factual first
    # clause and let the paragraph under the table state what the interval is.
    warning = k.warning.split(". ")[0] if k.warning else ""
    return {
        "cell": cell_id,
        "scope": scope,
        f"knee rps @ {slo_ms:g}ms": knee_s,
        "bootstrap interval": ci_s,
        "inferential?": valid_s,
        "rungs used": f"{k.n_rungs}" + (f" (-{k.n_rungs_dropped})" if k.n_rungs_dropped else ""),
        "note": k.note or warning,
    }


def _knee_table(cells: list[CellAnalysis], slo_ms: float) -> str:
    rows = []
    for c in cells:
        rows.append(_knee_row(c.cell_id, c.knee, slo_ms))
        for lang in _LANGS:
            k = c.knee_by_lang.get(lang)
            if k is not None:
                rows.append(_knee_row(c.cell_id, k, slo_ms))
    return _md(rows)


def _knee_ci_note(cells: list[CellAnalysis]) -> str:
    """One honest sentence about what the printed interval actually is.

    `ci_valid` is False for every fit in this run for a structural reason, not a
    bug: `analysis.knee.estimate_knee` marks the interval non-inferential when
    either rung framing the crossing carries fewer reps than
    `ladder.confirm_reps` (10). The ladder runs `reps_per_rung: 3`, and
    under-warmed exclusion thins the crossing bracket further, so the guard can
    never clear at the ladder stage. Fixing it would mean re-running the crossing
    bracket at confirm-stage rep counts; a measurement change, not an analysis
    change, so we state what the interval is instead of relabelling it.
    """
    brackets = []
    for c in cells:
        for k in [c.knee] + [c.knee_by_lang[lang] for lang in _LANGS if lang in c.knee_by_lang]:
            if k.knee_rps is not None and k.min_reps_in_bracket is not None:
                brackets.append(k.min_reps_in_bracket)
    worst = min(brackets) if brackets else 0
    return (
        f"The bracketed interval is the 2.5/97.5 percentile of 2000 isotonic refits "
        f"that resample the ladder's per-rung reps with replacement. It is NOT a "
        f"valid bootstrap CI here: the SLO-crossing bracket carries as few as "
        f"{worst} measurement-grade rep(s), against the pre-registered minimum of "
        f"`ladder.confirm_reps` = 10 (the ladder runs `reps_per_rung: 3`, and "
        f"under-warmed exclusion thins the bracket further). Read it as a spread "
        f"indicator over a handful of reps. An inferential CI needs the crossing "
        f"bracket re-run at confirm-stage rep counts, which this run did not do.\n"
    )


def _slo_table(cells: list[CellAnalysis], slo_ms: float) -> str:
    """Per-language p99 vs the SLO, with the verdict RULE named in the header.

    The verdict column is `mean(per-window p99) <= slo`, so the header says so
    and the neighbouring column prints how many windows individually met the
    threshold: the honest tail summary the mean can hide.

    The window column counts USED / TOTAL, never warm / total. A window is used
    when it passed the warm-up gates AND the CO-safety gates, so a bare
    "warm/total" label would claim a warm-up verdict over windows that were
    really dropped for error rate or rate fidelity. The neighbouring
    "why dropped" column names the actual reasons, one per dropped window.
    """
    rows = []
    for c in cells:
        for s in c.slo_at_rates:
            rows.append({
                "cell": c.cell_id,
                "stage": s.stage,
                "offered rps": s.rate,
                "language": _lang_label(s.lang),
                "mean p99 (ms)": f"{s.mean_p99_ms:.2f}",
                "worst window p99 (ms)": f"{s.max_p99_ms:.2f}",
                f"meets {slo_ms:g} ms? (mean rule)": "yes" if s.meets_slo else "NO",
                f"windows under {slo_ms:g} ms": f"{s.n_windows_met_slo}/{s.n_used}",
                "windows (used/total)": (
                    f"{0 if s.posture_only else s.n_used}/{s.n_total}"
                    + (" *posture*" if s.posture_only else "")
                ),
                "why dropped": _exclusion_summary(s.dropped_by),
            })
    return _md(rows)


def _slo_sentences(cells: list[CellAnalysis], slo_ms: float) -> list[str]:
    """Per-cell plain-language readout at the primary operating rate.

    Every "meets/MISSES" here is the mean-of-window-p99 rule, and it is
    qualified in place with the per-window met count and the worst window, so
    the sentence cannot be quoted as an unconditional tail guarantee.
    """
    out: list[str] = []
    for c in cells:
        rates = c.resource.operating_rates if (c.resource and c.resource.available) else []
        if not rates:
            continue
        rate = rates[0]
        bits = []
        for s in c.slo_at_rates:
            if s.stage != "confirm" or s.rate != rate:
                continue
            bits.append(
                f"{_lang_label(s.lang)} mean p99 {s.mean_p99_ms:.1f} ms "
                f"({'meets' if s.meets_slo else 'MISSES'} the {slo_ms:g} ms promise "
                f"on the mean rule; {s.n_windows_met_slo}/{s.n_used} windows "
                f"individually under it, worst {s.max_p99_ms:.1f} ms"
                + (", posture-only windows" if s.posture_only else "")
                + ")"
            )
        if bits:
            out.append(f"- **{c.cell_id}** at {rate} rps offered: " + "; ".join(bits) + ".")
    return out


def _cpu_vs_rate_table(c: CellAnalysis) -> str:
    rows = []
    for r in c.cpu_vs_rate.rungs:
        rows.append({
            "offered rps": r.rate,
            "Go CPU-ms/req": _fmt(r.go_cpu_ms_per_req, ".4f") + ("*" if r.go_posture_only else ""),
            "Go windows (used/total)": f"{r.go_n_warm}/{r.go_n_total}",
            ".NET CPU-ms/req": (
                _fmt(r.dotnet_cpu_ms_per_req, ".4f") + ("*" if r.dotnet_posture_only else "")
            ),
            ".NET windows (used/total)": f"{r.dotnet_n_warm}/{r.dotnet_n_total}",
            ".NET : Go": _fmt(r.ratio_dotnet_over_go, ".2f") + ("x" if r.ratio_dotnet_over_go else ""),
        })
    return _md(rows)


def _paired_table(cells: list[CellAnalysis]) -> str:
    """Render the per-rate paired verdicts as markdown.

    The header states the CONFIDENCE LEVEL of the interval (95%); the
    pre-registered +-equivalence margin is its own column, because they are two
    different numbers and conflating them (the old `f"{margin_pct}% CI"` header)
    made a 95% interval read as a "5% CI".
    """
    rows = []
    for c in cells:
        for p in c.paired_by_rate:
            rows.append({
                "cell": c.cell_id,
                "offered rps": p.rate if p.rate is not None else "n/a",
                "metric": p.metric,
                "blocks (n)": p.n_blocks,
                "Go": f"{p.mean_go:.4g}",
                ".NET": f"{p.mean_dotnet:.4g}",
                "diff %": f"{p.mean_diff_pct:+.2f}%",
                f"{p.ci_pct:g}% CI (paired t)": f"[{p.ci_low_pct:+.2f}, {p.ci_high_pct:+.2f}]",
                "equiv. margin": f"+-{p.margin_pct:g}%",
                "verdict": {"tie": "tie (within +-margin)", "go_better": "Go better",
                            "dotnet_better": ".NET better"}.get(p.verdict, p.verdict),
                "effect (d_z)": f"{p.effect_size:.2f}",
            })
    return _md(rows)


def _windows_cell(lr: LangResource) -> str:
    """"used / excluded" counts, or the all-flagged posture wording, never a bare n."""
    if lr.posture_only:
        return f"{lr.n_windows} / 0 (ALL {lr.n_windows} flagged; used as posture)"
    return f"{lr.n_windows} / {lr.n_flagged_excluded}"


def _stage_mix_cell(lr: LangResource) -> str:
    """The confirm/soak composition of the windows behind this language's number.

    Published because the two languages' aggregated window sets are NOT
    guaranteed to share one, and a soak window is an order of magnitude longer
    than a confirm window: without this row, a ratio between two differently
    composed samples reads as a like-for-like comparison.
    """
    if not lr.stage_windows:
        return "n/a"
    return ", ".join(f"{n} {stage}" for stage, n in sorted(lr.stage_windows.items()))


def _resource_table(cells: list[CellAnalysis]) -> str:
    """Go-vs-.NET steady-state resource cost per cell (cgroup v2 ground truth)."""
    rows = []
    for c in cells:
        r = c.resource
        if not r or not r.available:
            continue
        g, d = r.go, r.dotnet
        mark = {"go": "*" if g.posture_only else "", "dotnet": "*" if d.posture_only else ""}
        rows.append({"cell": c.cell_id, "metric": "CPU-ms / request",
                     "Go": f"{g.cpu_ms_per_req:.3f}{mark['go']}",
                     ".NET": f"{d.cpu_ms_per_req:.3f}{mark['dotnet']}",
                     ".NET : Go": f"{r.cpu_ratio:.2f}x"})
        rows.append({"cell": c.cell_id, "metric": "anon memory p99 (MB, in-window)",
                     "Go": f"{g.anon_p99_mb:.0f}{mark['go']}",
                     ".NET": f"{d.anon_p99_mb:.0f}{mark['dotnet']}",
                     ".NET : Go": f"{r.anon_ratio:.2f}x"})
        rows.append({"cell": c.cell_id, "metric": "cgroup memory.peak (MB, container lifetime)",
                     "Go": f"{g.mem_peak_mb:.0f}{mark['go']}",
                     ".NET": f"{d.mem_peak_mb:.0f}{mark['dotnet']}",
                     ".NET : Go": f"{r.peak_ratio:.2f}x"})
        rows.append({"cell": c.cell_id, "metric": "windows used / flagged-excluded",
                     "Go": _windows_cell(g), ".NET": _windows_cell(d),
                     ".NET : Go": "-"})
        rows.append({"cell": c.cell_id, "metric": "stage mix of the windows used",
                     "Go": _stage_mix_cell(g), ".NET": _stage_mix_cell(d),
                     ".NET : Go": "-"})
        rows.append({"cell": c.cell_id, "metric": "requests serviced (the CPU denominator)",
                     "Go": f"{g.requests}", ".NET": f"{d.requests}",
                     ".NET : Go": "-"})
    return _md(rows)


# Cell knobs that make a specific warmup gate unsatisfiable BY CONSTRUCTION, per
# language. Keyed (lang, env var, value) -> (gate name, why). Only a knob this
# table names may license a causal clause in a posture note; every other flagged
# gate is reported as an unexplained failure rather than attributed to a cause
# the artifacts do not support.
_KNOB_EXPLAINS_GATE: dict[tuple[str, str, str], tuple[str, str]] = {
    ("go", "GOGC", "off"): (
        "gc_steady",
        "this cell sets `GOGC=off` for Go, so there is no GC cadence to settle "
        "and the gate cannot be satisfied by construction",
    ),
}


def _posture_notes(cells: list[CellAnalysis]) -> list[str]:
    """Explicit per-language callouts where EVERY window in a roll-up was flagged.

    The CAUSE is derived, never asserted: a causal clause is emitted only when
    this cell's own declared `extra_env` (`phases.CELLS`) contains a knob that
    `_KNOB_EXPLAINS_GATE` says makes that specific gate unsatisfiable, AND that
    gate actually appears in the window's failed-gate counts. Gates that fail
    without such a knob are named as unexplained, so the note can never
    manufacture an explanation for a cell whose knobs do not imply one.
    """
    notes: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available:
            continue
        env = _cell_extra_env(c.cell_id)
        for lang, lr in (("go", r.go), ("dotnet", r.dotnet)):
            if not lr.posture_only:
                continue
            counts = lr.flagged_gate_counts
            gates = ", ".join(
                f"{name} {cnt}x" for name, cnt in sorted(counts.items())
            ) or "no per-window gate detail on disk"

            explained: list[str] = []
            reasons: list[str] = []
            for (klang, var, val), (gate, why) in _KNOB_EXPLAINS_GATE.items():
                if klang != lang or env.get(var, "").lower() != val:
                    continue
                if gate in counts:
                    explained.append(gate)
                    reasons.append(
                        f"`{gate}` failed in {counts[gate]} of {lr.n_windows} "
                        f"windows and {why}"
                    )
            unexplained = sorted(set(counts) - set(explained))

            if reasons:
                cause = " " + "; ".join(reasons) + "."
            elif counts:
                cause = (
                    " No knob declared for this cell explains these failures, so "
                    "no cause is claimed here; the gate counts above are the "
                    "whole of the evidence."
                )
            else:
                cause = ""
            residual = (
                " The remaining flagged gate(s), "
                + ", ".join(f"`{g}` {counts[g]}x" for g in unexplained)
                + ", are NOT explained by that knob."
                if reasons and unexplained else ""
            )
            notes.append(
                f"- **{c.cell_id} / {_lang_label(lang)}: all {lr.n_windows} probe "
                f"windows for this language were flagged under-warmed** (failed "
                f"gates: {gates})." + cause + residual +
                " These numbers are therefore reported as POSTURE EVIDENCE, not as "
                "a gated measurement, and the row is marked `*`."
            )
    return notes


def _stage_mix_sentences(cells: list[CellAnalysis]) -> list[str]:
    """Disclose (and quantify) any confirm/soak asymmetry behind a CPU ratio.

    The two languages' surviving window sets are built by the same exclusion but
    need not have the same stage composition: a flagged soak window drops one
    language's longest window and keeps the other's. A soak window services
    roughly 5x the requests of a confirm window, so under ANY weighting the two
    published numbers then average different stage mixes. Rather than leave that
    implicit, the asymmetry is named and the alternative weighting's ratio is
    printed beside the published one, so a reader can see exactly what the
    choice is worth.
    """
    out: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available or r.cpu_ratio is None:
            continue
        go_mix, dn_mix = r.go.stage_windows, r.dotnet.stage_windows
        if go_mix == dn_mix:
            continue
        alt = (
            f"Measured effect on the published ratio: the equal-weight-per-window "
            f"alternative gives {r.cpu_ratio_unweighted:.3f}x against the published "
            f"request-weighted {r.cpu_ratio:.3f}x, so the weighting choice does not "
            f"carry this cell's headline, but it is the reader's to check, not ours "
            f"to leave implicit."
            if r.cpu_ratio_unweighted is not None else
            "The alternative weighting could not be computed for this cell."
        )
        out.append(
            f"\n**Stage-mix asymmetry in {c.cell_id}.** The windows behind the two "
            f"CPU figures do not have the same stage composition: Go "
            f"{_stage_mix_cell(r.go)} vs .NET {_stage_mix_cell(r.dotnet)} (the "
            f"missing windows were flagged under-warmed, not omitted by choice). A "
            f"soak window is the run's longest probe and services several times a "
            f"confirm window's requests, so the two figures aggregate different stage "
            f"mixes. " + alt + "\n"
        )
    return out


def _per_lang_knee_sentences(cells: list[CellAnalysis]) -> list[str]:
    """Connect the roll-up's rate filter to each language's OWN knee.

    The filter is `rate <= the POOLED knee`. Where a language's own knee is
    below an aggregated rate, every window of that language in the roll-up is
    SUPRA-KNEE for it; the per-language knee table already shows this, but the
    resource section is where the consequence lands, so it is stated here too.
    """
    out: list[str] = []
    for c in cells:
        r = c.resource
        if not r or not r.available or not r.operating_rates:
            continue
        bits: list[str] = []
        for lang in _LANGS:
            k = c.knee_by_lang.get(lang)
            if k is None:
                continue
            if k.crossed and k.knee_rps is not None:
                supra = [rt for rt in r.operating_rates if rt > k.knee_rps]
                if supra:
                    bits.append(
                        f"{_lang_label(lang)}'s own knee is {k.knee_rps:.0f} rps, "
                        f"below the aggregated rate(s) {supra} rps, so every "
                        f"{_lang_label(lang)} window in this roll-up is supra-knee "
                        f"for {_lang_label(lang)}"
                    )
                else:
                    bits.append(
                        f"{_lang_label(lang)}'s own knee is {k.knee_rps:.0f} rps, at "
                        f"or above every aggregated rate"
                    )
            elif k.max_rung_rps is not None:
                bits.append(
                    f"{_lang_label(lang)} never crossed the SLO inside the ladder "
                    f"(top rung {k.max_rung_rps:.0f} rps at p99 "
                    f"{k.max_rung_p99_ms:.2f} ms), so no per-language knee bounds "
                    f"its rows"
                )
        if bits:
            out.append(
                f"\nPer-language cross-check for **{c.cell_id}**: the windows above "
                f"were selected by `rate <= the POOLED knee`, which is not by "
                f"construction any language's own latency promise. Against each "
                f"language's OWN knee: " + "; ".join(bits) + ".\n"
            )
    return out


# --------------------------------------------------------------------------- #
# report body
# --------------------------------------------------------------------------- #
def _build_report_md(
    run_id: str, slo_ms: float, cells: list[CellAnalysis], led: Ledger
) -> str:
    parts: list[str] = [f"# Ledgerline analysis report - run `{run_id}`\n"]

    parts.append(
        "## Knee fits (isotonic p99-vs-rate crossing "
        f"{slo_ms:g} ms): pooled AND per language\n"
    )
    parts.append(
        "The runner brackets the crossing on p99 POOLED over go and .NET probes, and "
        "that pooled knee is what selected the confirm rates. Pooling happens AFTER "
        "under-warmed exclusion, which is language-asymmetric, so what a \"pooled\" "
        "row contains is a fact about the data and is stated per row: where both "
        "languages survive, the fit is an ensemble crossing and not either language's "
        "knee; where only one survives, the fit IS that language's knee. The "
        "per-language rows refit the same ladder rungs one language at a time.\n"
    )
    parts.append(_knee_table(cells, slo_ms))
    one_lang = [
        (c.cell_id, (c.knee.composition or {}))
        for c in cells
        if c.knee.knee_rps is not None
        and len((c.knee.composition or {}).get("languages_present", _LANGS)) == 1
    ]
    if one_lang:
        parts.append(
            "\n" + "\n".join(
                f"- **{cid}: the \"pooled\" knee is a "
                f"{_lang_label(comp['languages_present'][0])}-only fit.** After "
                f"under-warmed exclusion its "
                f"{comp['n_rungs']} fitted rungs carry "
                + ", ".join(
                    f"{comp['warm_probes'][lg]} warm {_lang_label(lg)} probe(s)"
                    for lg in _LANGS
                )
                + ". This cell's confirm rates, and therefore its operating rate and "
                "everything aggregated at it below, were selected by a fit that saw "
                "one language only."
                for cid, comp in one_lang
            ) + "\n"
        )
    if any(k.knee_rps is not None
           for c in cells for k in [c.knee, *c.knee_by_lang.values()]):
        parts.append("\n" + _knee_ci_note(cells))

    parts.append(f"\n## The latency promise at the offered rate (p99 vs {slo_ms:g} ms SLO)\n")
    parts.append(
        "Both stacks were held to the same OFFERED RATE, never to the same latency "
        "promise. A rate that is sub-knee for the pooled fit can be supra-knee for one "
        "of the two languages, so each language's own measured p99 is printed against "
        f"the {slo_ms:g} ms SLO here.\n"
    )
    parts.append(
        f"THE VERDICT RULE: the `meets {slo_ms:g} ms?` column compares the MEAN of the "
        f"aggregated windows' p99 values against {slo_ms:g} ms. That is the same "
        f"rung-mean rule the ladder bracketed on, and it is lenient for a tail promise: "
        f"a language can be marked `yes` while individual windows miss badly. So the "
        f"worst window and the count of windows whose OWN p99 was under {slo_ms:g} ms "
        f"are printed in the same row: read those three columns together, never the "
        f"verdict alone.\n"
    )
    parts.append(_slo_table(cells, slo_ms))
    sentences = _slo_sentences(cells, slo_ms)
    if sentences:
        parts.append("\n" + "\n".join(sentences) + "\n")

    parts.append("\n## CPU-ms per request vs offered rate (is the cost rate-independent?)\n")
    parts.append(
        "A per-request cost quoted at one operating point is only a stack property if "
        "it does not move with rate. It does move. Each row is a ladder rung; a value "
        "is that rung's TOTAL cgroup CPU delta divided by the TOTAL requests its "
        "windows serviced (the same request-weighted rule the roll-up below uses). "
        "`*` = no measurement-grade window at that rung for that language, so the "
        "flagged windows were used (posture only).\n"
    )
    for c in cells:
        cvr = c.cpu_vs_rate
        if not cvr or not cvr.available:
            continue
        parts.append(f"\n**{c.cell_id}**\n")
        parts.append(_cpu_vs_rate_table(c))
        parts.append(
            f"\nCoefficient of variation across the {len(cvr.rungs)} rungs: "
            f"Go {_fmt(cvr.cv_go_pct, '.1f')}%, .NET {_fmt(cvr.cv_dotnet_pct, '.1f')}%. "
            f".NET:Go ratio spans {_fmt(cvr.ratio_min, '.2f')}x to "
            f"{_fmt(cvr.ratio_max, '.2f')}x across the ladder"
            + (f"; {cvr.n_posture_only_values} rung-values are posture-only.\n"
               if cvr.n_posture_only_values else ".\n")
        )
    if not any(c.cpu_vs_rate and c.cpu_vs_rate.available for c in cells):
        parts.append(
            "_Not available in this run: no per-window cgroup summaries under "
            "`cells/<cell>/ladder`._\n"
        )

    parts.append("\n## Stationarity (per-second p99, Mann-Kendall + Theil-Sen band)\n")
    parts.append(_stationarity_table(cells))

    parts.append("\n## Paired go-vs-.NET p99 verdict (confirm stage, RCB-paired by block)\n")
    paired = [c for c in cells if c.paired_by_rate]
    if paired:
        parts.append(
            "One verdict PER OFFERED RATE: confirm runs at both bracket rates, and a "
            "verdict pooled over both would average two different operating points. "
            "The interval is the paired-t interval on the per-block differences at its "
            "stated confidence level; the equivalence margin is the separate, "
            "pre-registered +-band the interval is compared against. The verdict word is "
            "that equivalence decision (interval not contained in +-margin -> direction "
            "of the mean difference); an interval that ALSO spans 0% is not a "
            "significant difference at its confidence level, so read the interval, not "
            "only the word.\n"
        )
        parts.append(_paired_table(paired))
    else:
        parts.append(
            "_Not available in this run: no completed confirm-stage paired blocks "
            "(the paired difference needs >=2 RCB blocks with both go and .NET warm)._\n"
        )
    missing = [c.cell_id for c in cells if not c.paired_by_rate]
    if missing and paired:
        parts.append(
            "\nNo paired verdict for: " + ", ".join(missing) +
            "; a block contributes a pair only when BOTH languages are "
            "measurement-grade in it, and in the cell(s) listed one language has no warm "
            "confirm window at all (see the posture note below).\n"
        )

    parts.append("\n## Resource cost at the operating rate (SUT cgroup v2 ground truth)\n")
    res_cells = [c for c in cells if c.resource and c.resource.available]
    if res_cells:
        rates = sorted({r for c in res_cells for r in c.resource.operating_rates})
        parts.append(
            f"Steady-state SUT cgroup CPU + memory per serviced request at the offered "
            f"rate(s) {rates} rps (selected as rate <= the POOLED knee), aggregated over "
            f"the confirm + soak probe windows. Under-warmed / gate-excluded windows are "
            f"EXCLUDED, the same exclusion the knee fit applies; the counts used vs "
            f"excluded are a row of the table. `.NET : Go` > 1 means .NET spends more. "
            f"Per-request CPU is NOT rate-independent (see the CPU-vs-rate section), so "
            f"these are values AT these rates, not stack constants.\n"
        )
        parts.append(
            "THE AGGREGATION RULE: `CPU-ms / request` is REQUEST-WEIGHTED: the total "
            "cgroup CPU delta of the windows in the roll-up divided by the total "
            "requests they serviced, so the request count in the table really is its "
            "denominator. The memory rows are plain means over the same windows (a "
            "memory level is not a per-request quantity, so there is nothing to weight "
            "it by).\n"
        )
        parts.extend(_stage_mix_sentences(res_cells))
        parts.append(_resource_table(res_cells))
        parts.extend(_per_lang_knee_sentences(res_cells))
        notes = _posture_notes(res_cells)
        if notes:
            parts.append("\n" + "\n".join(notes) + "\n")
        parts.append(
            "\n`anon memory p99` is the p99 of `memory.stat anon` sampled inside the "
            "measurement windows. `cgroup memory.peak` is the kernel's high-water mark "
            "of `memory.current` over the CONTAINER'S WHOLE LIFETIME: anonymous memory "
            "plus page cache plus kernel memory, including warmup, so it is not an "
            "in-window RSS figure and is not comparable to the anon p99 above it.\n"
        )
        pool_bits = []
        for c in res_cells:
            r = c.resource
            pool_bits.append(
                f"{c.cell_id}: Go {r.go.db_pool_total}/{r.go.db_pool_max}, "
                f".NET {r.dotnet.db_pool_total}/{r.dotnet.db_pool_max}"
            )
        parts.append(
            "\nPool warmth at the end of warmup (open connections / configured max): "
            + "; ".join(pool_bits) + ".\n"
        )
        throttle_bad = [
            f"{c.cell_id}/{lang}"
            for c in res_cells
            for lang, lr in (("go", c.resource.go), ("dotnet", c.resource.dotnet))
            if not lr.throttle_clean
        ]
        oom_bad = [
            f"{c.cell_id}/{lang}"
            for c in res_cells
            for lang, lr in (("go", c.resource.go), ("dotnet", c.resource.dotnet))
            if not lr.oom_clean
        ]
        n_windows = sum(c.resource.go.n_windows + c.resource.dotnet.n_windows for c in res_cells)
        parts.append(
            f"\nResource-accounting validity, over the {n_windows} aggregated "
            f"confirm + soak windows in the table above (NOT over the ladder windows, "
            f"which are not aggregated here): CPU throttle counters "
            + ("zero" if not throttle_bad else "NON-ZERO in " + ", ".join(throttle_bad))
            + "; cgroup OOM events "
            + ("zero" if not oom_bad else "NON-ZERO in " + ", ".join(oom_bad))
            + ". A throttled window would deflate CPU-ms/request; an OOM window would "
            "invalidate the memory figures.\n"
        )
        served = "; ".join(
            f"{c.cell_id}: Go {c.resource.go.requests} requests "
            f"({c.resource.go.non_2xx} non-2xx) over {c.resource.go.cpu_ms_total:.0f} "
            f"CPU-ms, .NET {c.resource.dotnet.requests} "
            f"({c.resource.dotnet.non_2xx} non-2xx) over "
            f"{c.resource.dotnet.cpu_ms_total:.0f} CPU-ms"
            for c in res_cells
        )
        parts.append(
            f"\nThe two quantities the CPU rows divide: total cgroup CPU over the "
            f"aggregated windows, and the requests those windows serviced: {served}. "
            f"Dividing the second into the first reproduces the `CPU-ms / request` row "
            f"exactly; that is the aggregation rule stated above.\n"
        )
    else:
        parts.append(
            "_Not available in this run: no SUT cgroup-v2 resource summaries on "
            "disk (Windows/smoke run, or a run whose per-window cells tree was not "
            "retained)._\n"
        )

    parts.append("\n## Not captured in this run (scope)\n")
    parts.extend(_scope_bullets(led, res_cells))
    return "\n".join(parts) + "\n"


def _stationarity_table(cells: list[CellAnalysis]) -> str:
    rows = []
    for c in cells:
        s = c.stationarity
        if not s.get("available"):
            verdict = "not available in this run"
        else:
            n_fail = sum(1 for v in s["per_probe_fails"].values() if v)
            verdict = (
                f"{n_fail}/{s['n_series']} series non-stationary"
                if s["any_fail"] else f"all {s['n_series']} series stationary"
            )
        rows.append({"cell": c.cell_id, "stationarity": verdict})
    return _md(rows)


def _scope_bullets(led: Ledger, res_cells: list[CellAnalysis]) -> list[str]:
    """The honest "what this run does NOT establish" list.

    Everything here is derived from the run's own artifacts (the ledger's phase
    records, the loaded spec, the aggregated status codes) rather than asserted,
    so the section cannot drift away from what actually happened.
    """
    bullets: list[str] = []

    # --- capacity gate: read the recorded verdict, do not assume it passed ---- #
    cap = led.phases.get("capacity_gate")
    verdict_json = _read_json(paths.run_dir(led.run_id) / "capacity-gate" / "verdict.json") or {}
    if cap is not None and cap.data:
        verdict = verdict_json.get("verdict") or cap.data.get("verdict")
        levers = verdict_json.get("levers_fired") or cap.data.get("levers_fired") or []
        lever_names = ", ".join(
            lv.get("name", "?") if isinstance(lv, dict) else str(lv) for lv in levers
        ) or "none"
        capacity = verdict_json.get("pg_knee_rps", cap.data.get("pg_knee_rps"))
        projected = verdict_json.get("projected_db_rps")
        headroom = verdict_json.get("headroom_factor")
        n_rungs = len(verdict_json.get("rungs") or [])
        realized = []
        for c in res_cells:
            for lang, lr in (("Go", c.resource.go), (".NET", c.resource.dotnet)):
                if lr.create_share_pct is not None:
                    realized.append(f"{c.cell_id}/{lang} {lr.create_share_pct:.2f}%")
        try:
            spec_share = load_workload().mix.create_invoice
        except Exception:  # noqa: BLE001 - spec unreadable (isolated test tree)
            spec_share = None
        if verdict and verdict != "pass":
            bullets.append(
                f"- **The capacity gate did NOT pass.** Recorded verdict `{verdict}`"
                + (
                    f": PG capacity {capacity:.0f} rps against a projected DB load of "
                    f"{projected:.0f} rps"
                    + (f" at headroom x{headroom:g}" if isinstance(headroom, (int, float)) else "")
                    if isinstance(capacity, (int, float)) and isinstance(projected, (int, float))
                    else ""
                )
                + f". Levers fired: {lever_names}. This is the exhausted-fallback branch: "
                f"every pre-registered lever was tried and the required DB headroom was "
                f"still not demonstrated, yet the cells phase proceeded and this report "
                f"was produced anyway; nothing gates on it. The gate's persisted verdict "
                f"records {n_rungs} create-only rung(s), so the capacity figure it rests "
                f"on is not a fitted knee. The lever values were held in the gate's own "
                f"verdict and were NOT written back into the workload the cells executed"
                + (
                    ": the realized create share over the aggregated windows is "
                    + ", ".join(realized)
                    + (f", i.e. the frozen spec mix ({spec_share}%), not the gate's "
                       f"lowered write-share lever." if spec_share is not None else ".")
                    if realized else "."
                )
                + " Read every rate in this report as measured against a DB whose "
                "headroom was never established.\n"
            )

    # --- descoped validation checks (pre-freeze ledgers only) ------------- #
    # Runs recorded before the descope carry {"deferred": ...} entries for the
    # since-removed bench-tier checks; surface them for those runs. New runs
    # record none (the descope note lives in docs/METHODOLOGY.md).
    val = led.phases.get("validation")
    if val is not None:
        deferred = [
            k for k in ("co", "logging_sink", "conn_sweep")
            if isinstance(val.data.get(k), dict) and "deferred" in val.data[k]
        ]
        if deferred:
            bullets.append(
                "- **Validation checks recorded as deferred (since descoped).** This run "
                "recorded " + ", ".join(f"`{k}`" for k in deferred)
                + " as deferred; the CO suite, oha tail cross-check, logging-sink "
                "microbench and connection sweep were removed from scope at the freeze "
                "and never ran. The numbers here are not gated by them; CO safety "
                "rests on the per-probe achieved-vs-offered evidence.\n"
            )

    # --- warmup gates declared in the spec but not implemented ---------------- #
    # Source of truth for "implemented" is the runner's own canonical registry
    # (`warmup.IMPLEMENTED_GATES`), imported rather than restated, so this bullet
    # cannot drift if a gate is implemented later. `per_endpoint_min_calls` is
    # NOT one of them: it is a separate spec key (`warmup.per_endpoint_min_calls`)
    # that the runner always evaluates, so it is named separately instead of
    # being smuggled into the declared-gate arithmetic.
    try:
        declared = set(load_slo().warmup.gates)
    except Exception:  # noqa: BLE001
        declared = set()
    unimplemented = sorted(declared - set(IMPLEMENTED_GATES))
    evaluated = sorted(declared & set(IMPLEMENTED_GATES))
    # Cross-check the claim against the run's own per-probe records: a gate the
    # registry calls unimplemented must never appear as a MEASURED failure.
    observed: set[str] = set()
    for c in res_cells:
        for lr in (c.resource.go, c.resource.dotnet):
            observed.update(lr.flagged_gate_counts)
    contradicted = sorted(observed & set(unimplemented))
    if unimplemented:
        n = len(unimplemented)
        bullets.append(
            "- **Warmup gates declared in `spec/slo.yaml` but NOT implemented:** "
            + ", ".join(f"`{g}`" for g in unimplemented)
            + f". The runner evaluates {', '.join(f'`{g}`' for g in evaluated)} "
            f"(`ledgerbench.warmup.IMPLEMENTED_GATES`) plus the separate "
            f"`per_endpoint_min_calls` spec key, so a probe recorded as warm was never "
            f"checked against the {n} gate(s) named above. Their absence is invisible "
            f"in the per-probe warmup records; it is stated here instead. "
            + (
                "Cross-check against this run's own records: the gates its flagged "
                "windows report failing are "
                + (", ".join(f"`{g}`" for g in sorted(observed)) if observed else "none")
                + ", none of which is in the unimplemented list.\n"
                if not contradicted else
                "INCONSISTENT: this run's flagged windows report "
                + ", ".join(f"`{g}`" for g in contradicted)
                + " as failing, yet the registry says that gate is not implemented: "
                "trust the per-probe records over this bullet and fix the registry.\n"
            )
        )

    # --- effective working set / cache residency ------------------------------ #
    try:
        wl = load_workload()
        hit_rate = wl.cache_construction.target_hit_rate
        inv_hi = wl.keyspaces.invoices.seeded_range[1]
        cust_hi = wl.keyspaces.customers.seeded_range[1]
    except Exception:  # noqa: BLE001
        hit_rate = inv_hi = cust_hi = None
    scale = led.scale or "full"
    # GROUND TRUTH first: the manifest's working_set counts what the generated
    # targets file actually contained. Re-resolving ScaleProfile here would read
    # the ANALYZE-TIME environment (LEDGERBENCH_TARGET_COUNT), which can differ
    # from what the run used; it is only a labeled fallback.
    from .manifest import RunManifest
    try:
        manifest = RunManifest.load(led.run_id)
    except Exception:  # noqa: BLE001
        manifest = None
    ws = getattr(manifest, "working_set", None)
    target_count = getattr(ws, "target_count", None)
    target_count_src = "manifest `working_set.target_count`, measured from the generated file"
    if target_count is None:
        try:
            target_count = phases.ScaleProfile.resolve(scale, load_slo()).target_count
            target_count_src = (
                f"`ScaleProfile.{scale}.target_count` re-resolved at ANALYZE time: "
                "assumed, not measured (manifest recorded no working_set)"
            )
        except Exception:  # noqa: BLE001
            target_count = None
    cache_stats = getattr(manifest, "cache_stats", None)
    measured_hit = None
    if isinstance(cache_stats, dict) and cache_stats.get("hit_rate") is not None:
        measured_hit = float(cache_stats["hit_rate"])
    elif cache_stats is not None and getattr(cache_stats, "hit_rate", None) is not None:
        measured_hit = float(cache_stats.hit_rate)
    if target_count is not None:
        hit_sentence = (
            f" The REALIZED Redis hit rate this run was {measured_hit:.3f} "
            "(manifest `cache_stats`, INFO keyspace deltas over measured windows)."
            if measured_hit is not None else
            " The actual hit rate was NOT measured this run (the manifest's "
            "`cache_stats` are null), so any claim that rests on cache-miss "
            "behaviour, notably the EF Core cache-miss round-trip asymmetry, "
            "is not supported by this run's evidence."
        )
        bullets.append(
            f"- **Effective working set and cache residency.** The load replays a "
            f"FIXED pre-generated target list of {target_count} requests "
            f"({target_count_src}), drawn Zipf-hot"
            + (f" from {inv_hi} seeded invoices / {cust_hi} seeded customers"
               if inv_hi and cust_hi else "")
            + ". The distinct entity ids it touches are a fraction of the seeded "
            "corpus and the list replays against the cache TTL at the operating "
            "rates above, so the read path is more cache-resident than the "
            "constructed target hit rate"
            + (f" of {hit_rate:.2f}" if hit_rate is not None else "")
            + " implies." + hit_sentence + "\n"
        )

    # --- pre-existing scope notes -------------------------------------------- #
    bullets.append(
        "- **DB-CPU-per-request attribution.** Only the two SUT containers are "
        "cgroup-sampled; the PostgreSQL CPU delta that would split app cost from "
        "query-plan cost is not captured (descoped at scope freeze), so it is omitted "
        "rather than estimated. The CO-safety of the load rests on the per-probe "
        "achieved==offered evidence (manifest `achieved_vs_offered`).\n"
    )
    bullets.append(
        "- **/runtime-stats GC counters.** The 1 Hz runtime-stats poller drives the "
        "warmup gates live; its per-window snapshots are not flushed to disk "
        "(descoped), so GC collection counts / pause totals are unavailable; only the "
        "`gc_steady` warmup gate (pass/fail) and the warmed pool size survive.\n"
    )
    bullets.append(
        "- **Hysteresis, p999 order-statistic CIs, isolation runs, and PG/vegeta/dockerd "
        "cgroup deltas.** Not captured; removed from scope at the freeze (see "
        "docs/METHODOLOGY.md).\n"
    )
    bullets.append(
        "- **Illustrative $/M-request cost.** Not published as a dollar figure: it would "
        "only scale the CPU ratio above by a chosen cloud vCPU price, and that ratio is "
        "itself rate-dependent (see the CPU-vs-rate section).\n"
    )
    return bullets


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def analyze_run(run_id: str) -> AnalyzeResult:
    """Load the run's ledger, analyze every cell with ladder data, write artifacts.

    Returns the assembled result (with `analyzable=False` and an empty `cells`
    list when no cell has a completed ladder). Always writes fits.json + report.md
    so the caller can point a reader at them even on the no-data path.
    """
    led = Ledger.load(run_id)
    if led is None:
        raise FileNotFoundError(f"no ledger at {paths.run_dir(run_id) / 'ledger.json'}")

    slo = load_slo()
    slo_ms = float(slo.slo.threshold_ms)

    cells: list[CellAnalysis] = []
    for cell_rec in led.cells:
        analysis = _analyze_cell(cell_rec, slo, run_id)
        if analysis is not None:
            cells.append(analysis)

    report_dir = paths.run_dir(run_id) / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    fits_path = report_dir / "fits.json"
    report_path = report_dir / "report.md"

    fits = {
        "run_id": run_id,
        "slo_ms": slo_ms,
        "config_hash": led.config_hash,
        "cells": [c.to_dict() for c in cells],
    }
    paths.write_text_atomic(fits_path, json.dumps(fits, indent=2))
    paths.write_text_atomic(report_path, _build_report_md(run_id, slo_ms, cells, led))

    return AnalyzeResult(
        run_id=run_id, slo_ms=slo_ms, cells=cells,
        fits_path=fits_path, report_path=report_path,
    )
