"""Post-run analyze wiring: synthetic ledger -> fits.json + report.md.

Builds a small hand-computable ledger (1 cell, a 2-rung ladder whose isotonic
SLO crossing is exactly 1500 rps, plus a 3-block confirm stage) and asserts the
analyze entry point fits the expected knee bracket, emits the report table, and
exits cleanly on the no-data path. The knee math is verified independently in
bench/analysis/tests/test_knee.py; here we test the ledger -> artifact wiring.

The second half of this module pins the HONESTY behaviour the report depends on,
each with a hand-computable fixture:

  - under-warmed windows are excluded from the resource roll-up (and the used vs
    excluded counts are published), exactly as the fit input excludes them;
  - a language whose windows are ALL flagged is published as posture evidence,
    labelled as such, never as a gated measurement;
  - the knee is fitted per language as well as pooled, and a language that never
    crosses inside the ladder is reported with the ladder's top rung instead of
    an extrapolation;
  - the paired verdict carries the offered rate it was computed at;
  - the CPU-ms/request-vs-rate curve (with per-language CV) is emitted;
  - a "pooled" knee whose rungs kept ONE language's probes is described as that
    language's knee, not as an ensemble crossing;
  - CPU-ms/request is request-weighted (total CPU / total requests), the stage
    composition of the aggregated windows is published, and the SLO verdict
    states its mean-of-window-p99 rule alongside a per-window met count;
  - the all-flagged posture note derives its cause from the cell's OWN knobs and
    claims none when the knobs do not imply one.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

# analyze_run fits a knee via the scientific stack (`uv sync` installs it); skip
# the whole module (not just collect) if it is somehow absent, the way the
# analysis suite guards its own tests.
_MISSING = [m for m in ("numpy", "scipy", "pandas") if importlib.util.find_spec(m) is None]
pytestmark = pytest.mark.skipif(
    bool(_MISSING), reason=f"analysis stack missing: {', '.join(_MISSING)}"
)

from ledgerbench import analyze
from ledgerbench.ledger import (
    CellRecord,
    Ledger,
    ProbeRecord,
    State,
    StageRecord,
)
from ledgerbench.util import paths


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def _done_probe(rate: int, block: int, lang: str) -> ProbeRecord:
    return ProbeRecord(
        block=block, slot_order=["go", "dotnet"], lang=lang, rate=rate,
        state=State.DONE, artifact=f"r{rate}.b{block}.{lang}.json",
        achieved=float(rate), error_rate=0.0,
    )


def _ladder_stage(flagged_lang: str | None = None) -> StageRecord:
    """A 2-rung ladder whose isotonic p99 crosses 20 ms at exactly 1500 rps.

    rung 1000 rps -> pooled p99 10 ms; rung 2000 rps -> 30 ms. Both languages
    carry the same value per rung so the rung mean is exact and the linear
    crossing is hand-computable: 1000 + (20-10)/(30-10) * (2000-1000) = 1500.

    `flagged_lang` marks EVERY probe of one language under-warmed, which is the
    shape the run's gc-generous cell has: the pooled fit then keeps the other
    language's probes only.
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    series: dict[str, list[float]] = {}
    for rate, val in ((1000, 10.0), (2000, 30.0)):
        for block in (0, 1):
            for lang in ("go", "dotnet"):
                key = f"r{rate}.b{block}.{lang}"
                rec.probes.append(_done_probe(rate, block, lang))
                p99[key] = val
                under_warmed[key] = lang == flagged_lang
                # flat, stationary per-second series (no positive trend)
                series[key] = [5.0, 5.0, 5.0, 5.0, 5.0]
    rec.data = {
        "crossed": True,
        "bracket": [1000, 2000],
        "p99_ms": p99,
        "under_warmed": under_warmed,
        "p99_series": series,
        "rates": [1000, 2000],
    }
    return rec


def _confirm_stage() -> StageRecord:
    """A 3-block confirm at one rate; Go p99 ~10% below .NET so the paired verdict
    is decisive (Go better on a lower-is-better latency metric)."""
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    rate = 1500
    for block in range(3):
        for lang, val in (("go", 9.0), ("dotnet", 10.0)):
            key = f"r{rate}.b{block}.{lang}"
            rec.probes.append(_done_probe(rate, block, lang))
            p99[key] = val
            under_warmed[key] = False
    rec.data = {"p99_ms": p99, "under_warmed": under_warmed, "confirm_rates": [rate]}
    return rec


def _synthetic_ledger(run_id: str) -> Ledger:
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    cell = CellRecord(
        cell_id="headline.mixed.gc-default",
        ladder=_ladder_stage(),
        confirm=_confirm_stage(),
    )
    led.cells.append(cell)
    led.save()
    return led


def test_analyze_fits_expected_knee_bracket(isolated_results):
    _synthetic_ledger("syn-1")
    result = analyze.analyze_run("syn-1")

    assert result.analyzable
    assert len(result.cells) == 1
    knee = result.cells[0].knee
    assert knee.crossed is True
    # isotonic crossing of 20 ms between (1000,10) and (2000,30) is exactly 1500
    assert abs(knee.knee_rps - 1500.0) < 1.0
    # CI bounds straddle the point estimate
    assert knee.ci_low_rps <= knee.knee_rps <= knee.ci_high_rps

    # fits.json is machine-readable with the same numbers
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["run_id"] == "syn-1"
    assert fits["slo_ms"] == 20.0
    cell = fits["cells"][0]
    assert abs(cell["knee_pooled"]["knee_rps"] - 1500.0) < 1.0
    assert cell["knee_pooled"]["n_rungs"] == 2


def test_analyze_report_md_contains_tables(isolated_results):
    _synthetic_ledger("syn-2")
    result = analyze.analyze_run("syn-2")
    md = result.report_path.read_text(encoding="utf-8")
    assert "# Ledgerline analysis report" in md
    assert "Knee fits" in md
    assert "headline.mixed.gc-default" in md
    # the knee table renders the crossing rate
    assert "1500" in md
    # paired section renders the decisive go-better verdict
    assert "Go better" in md


def test_analyze_paired_verdict_from_confirm_blocks(isolated_results):
    _synthetic_ledger("syn-3")
    result = analyze.analyze_run("syn-3")
    paired = result.cells[0].paired
    assert paired is not None
    assert paired.n_blocks == 3
    # Go p99 (9) is 10% below .NET (10): lower-is-better -> Go better, decisive
    assert paired.verdict == "go_better"
    assert paired.mean_diff_pct < 0


def test_analyze_stationarity_summary_flat_series_passes(isolated_results):
    _synthetic_ledger("syn-4")
    result = analyze.analyze_run("syn-4")
    stat = result.cells[0].stationarity
    assert stat["available"] is True
    assert stat["any_fail"] is False  # flat series -> no positive trend


def test_no_crossing_ladder_reports_no_knee(isolated_results):
    led = Ledger(run_id="syn-nc", config_hash="testhash", rng_seed=42)
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate in (1000, 2000):
        for block in (0, 1):
            for lang in ("go", "dotnet"):
                key = f"r{rate}.b{block}.{lang}"
                rec.probes.append(_done_probe(rate, block, lang))
                p99[key] = 5.0  # sub-knee everywhere
                under_warmed[key] = False
    rec.data = {"crossed": False, "bracket": [2000], "p99_ms": p99,
                "under_warmed": under_warmed, "rates": [1000, 2000]}
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=rec))
    led.save()

    result = analyze.analyze_run("syn-nc")
    assert result.analyzable  # the cell IS analyzed (ladder ran), just no knee
    knee = result.cells[0].knee
    assert knee.crossed is False
    assert knee.knee_rps is None
    md = result.report_path.read_text(encoding="utf-8")
    assert "no crossing" in md


def test_no_analyzable_cells_when_no_ladder_done(isolated_results):
    led = Ledger(run_id="syn-empty", config_hash="testhash", rng_seed=42)
    # a cell whose ladder never completed -> not analyzable
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default"))
    led.save()
    result = analyze.analyze_run("syn-empty")
    assert result.analyzable is False
    assert result.cells == []
    # artifacts are still written (empty cells list) so the path is reportable
    assert result.fits_path.exists() and result.report_path.exists()


def test_missing_ledger_raises(isolated_results):
    with pytest.raises(FileNotFoundError):
        analyze.analyze_run("does-not-exist")


# --------------------------------------------------------------------------- #
# resource cost (cgroup CPU/mem per request, read from the cells/ tree)
# --------------------------------------------------------------------------- #
def _write_probe_window(
    blockdir, lang, *, reqs, cpu_us, anon, peak, under_warmed=False,
    failed_gates=(), throttled=0, oom=0,
):
    """Write the per-window cgroup + latency + warmup summaries one probe emits."""
    blockdir.mkdir(parents=True, exist_ok=True)
    (blockdir / f"cpu_mem_{lang}.summary.json").write_text(json.dumps({
        "container": f"ledgerline-sut-{lang}-1", "enabled": True,
        "cpu_usage_delta_usec": cpu_us, "nr_throttled_end": throttled,
        "throttled_usec_end": 0, "mem_anon_p99": anon, "mem_peak": peak,
        "mem_file_end": 0, "oom_events": oom, "note": "",
    }), encoding="utf-8")
    (blockdir / f"{lang}.summary.json").write_text(json.dumps({
        "latencies": {"99th": 9_000_000}, "requests": reqs, "success": 1,
        "status_codes": {"200": reqs}, "errors": [],
    }), encoding="utf-8")
    gates = ["per_endpoint_min_calls", "p99_flat", "p999_flat", "gc_steady", "pools_warm"]
    (blockdir / f"{lang}.warmup.json").write_text(json.dumps({
        "language": lang, "under_warmed": under_warmed,
        "final_db_pool_total": 24, "final_db_pool_max": 24,
        "gates": [{"name": g, "passed": g not in failed_gates} for g in gates],
    }), encoding="utf-8")


def test_resource_cost_from_cells_tree(isolated_results):
    """A sub-knee confirm window where .NET burns 3x the CPU and 5x the memory
    of Go is read from the cgroup summaries and rendered as a Go:.NET ratio."""
    run_id = "syn-res"
    _synthetic_ledger(run_id)  # knee crosses at 1500 rps
    cell = "headline.mixed.gc-default"
    # rate 1000 is sub-knee (<1500) so it is aggregated. go: 100 us/req over
    # 1000 reqs -> 0.1 CPU-ms/req; .NET: 300 us/req -> 0.3; anon 30 vs 150 MB.
    base = isolated_results / run_id / "cells" / cell / "confirm" / "rate1000"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, "go", reqs=1000, cpu_us=100_000, anon=30_000_000, peak=60_000_000)
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000, anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res is not None and res.available is True
    assert res.operating_rates == [1000]
    assert abs(res.go.cpu_ms_per_req - 0.1) < 1e-9
    assert abs(res.dotnet.cpu_ms_per_req - 0.3) < 1e-9
    assert abs(res.cpu_ratio - 3.0) < 1e-9
    assert abs(res.anon_ratio - 5.0) < 1e-9
    assert res.go.db_pool_total == 24 and res.go.db_pool_max == 24
    assert res.go.throttle_clean and res.go.oom_clean

    md = result.report_path.read_text(encoding="utf-8")
    assert "Resource cost at the operating rate" in md
    assert "3.00x" in md  # .NET : Go CPU ratio
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["cells"][0]["resource"]["available"] is True


def test_resource_cost_absent_when_no_cells_tree(isolated_results):
    """No cgroup summaries on disk (the default synthetic ledger) -> the report
    honestly says not-captured rather than fabricating a number."""
    _synthetic_ledger("syn-nores")
    result = analyze.analyze_run("syn-nores")
    res = result.cells[0].resource
    assert res is not None and res.available is False
    md = result.report_path.read_text(encoding="utf-8")
    assert "Not available in this run: no SUT cgroup-v2 resource summaries" in md


# --------------------------------------------------------------------------- #
# under-warmed exclusion: the roll-up must apply the SAME filter as the fit
# --------------------------------------------------------------------------- #
def _confirm_stage_at(rate: int, n_blocks: int, *, flagged: set[str] = frozenset()) -> StageRecord:
    """A confirm stage at ONE rate; `flagged` holds probe keys to mark under-warmed."""
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for block in range(n_blocks):
        for lang, val in (("go", 9.0), ("dotnet", 10.0)):
            key = f"r{rate}.b{block}.{lang}"
            rec.probes.append(_done_probe(rate, block, lang))
            p99[key] = val
            under_warmed[key] = key in flagged
    rec.data = {"p99_ms": p99, "under_warmed": under_warmed, "confirm_rates": [rate]}
    return rec


def _ledger_with_confirm(run_id: str, confirm: StageRecord) -> Ledger:
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(
        cell_id="headline.mixed.gc-default", ladder=_ladder_stage(), confirm=confirm,
    ))
    led.save()
    return led


def test_resource_rollup_excludes_under_warmed_windows(isolated_results):
    """A flagged window must not enter the CPU/memory means, and both counts
    (used vs excluded) must be published so the exclusion is visible.

    Three go windows at a sub-knee rate: two warm at 0.1 CPU-ms/req and one
    FLAGGED at 1.0. Including it would report 0.4; the honest number is 0.1.
    """
    run_id = "syn-uw"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 3, flagged={f"r{rate}.b2.go"}))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(3):
        bd = base / f"block{block}"
        flagged = block == 2
        _write_probe_window(
            bd, "go", reqs=1000, cpu_us=1_000_000 if flagged else 100_000,
            anon=30_000_000, peak=60_000_000, under_warmed=flagged,
            failed_gates=("gc_steady",) if flagged else (),
        )
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    res = analyze.analyze_run(run_id).cells[0].resource
    assert res.available is True
    assert res.go.n_windows == 2 and res.go.n_flagged_excluded == 1
    assert res.go.posture_only is False
    assert abs(res.go.cpu_ms_per_req - 0.1) < 1e-9      # NOT 0.4
    assert abs(res.cpu_ratio - 3.0) < 1e-9
    assert res.dotnet.n_windows == 3 and res.dotnet.n_flagged_excluded == 0

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "windows used / flagged-excluded" in md
    assert "2 / 1" in md


def test_all_flagged_language_is_published_as_posture_not_measurement(isolated_results):
    """Every go window flagged (the GOGC=off / gc_steady case): the numbers are
    still emitted, but marked posture evidence with the failed gate named."""
    run_id = "syn-posture"
    rate = 1000
    flagged = {f"r{rate}.b{b}.go" for b in range(2)}
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2, flagged=flagged))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, "go", reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000, under_warmed=True, failed_gates=("gc_steady",))
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.available is True
    assert res.go.posture_only is True and res.go.n_windows == 2
    assert res.go.flagged_gate_counts == {"gc_steady": 2}
    assert res.dotnet.posture_only is False

    md = result.report_path.read_text(encoding="utf-8")
    assert "all 2 probe windows for this language were flagged under-warmed" in md
    assert "POSTURE EVIDENCE, not as a gated measurement" in md
    assert "gc_steady 2x" in md
    # gc-default declares NO GC knob, so no cause may be manufactured for it
    assert "GOGC=off" not in md
    assert "No knob declared for this cell explains these failures" in md


def test_posture_note_derives_its_cause_from_the_cell_s_own_knobs(isolated_results):
    """The GOGC=off -> gc_steady explanation may be printed only for a cell that
    actually declares that knob, and only for the gate it explains.

    Same fixture as above but in the gc-generous cell (`GOGC=off` in
    `phases.CELLS`), with a second failed gate the knob does NOT explain.
    """
    run_id = "syn-posture-knob"
    rate = 1000
    flagged = {f"r{rate}.b{b}.go" for b in range(2)}
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(
        cell_id="headline.mixed.gc-generous", ladder=_ladder_stage(),
        confirm=_confirm_stage_at(rate, 2, flagged=flagged),
    ))
    led.save()
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-generous" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(
            bd, "go", reqs=1000, cpu_us=100_000, anon=30_000_000, peak=60_000_000,
            under_warmed=True,
            failed_gates=("gc_steady", "p99_flat") if block == 0 else ("gc_steady",),
        )
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "this cell sets `GOGC=off` for Go" in md
    assert "`gc_steady` failed in 2 of 2 windows" in md
    # the unrelated gate is named as unexplained, not swept into the GC story
    assert "The remaining flagged gate(s), `p99_flat` 1x, are NOT explained" in md


# --------------------------------------------------------------------------- #
# per-language knee fits alongside the pooled one
# --------------------------------------------------------------------------- #
def _split_ladder_stage() -> StageRecord:
    """3 rungs where .NET crosses the SLO inside the ladder and go never does.

    rung -> (go p99, .NET p99): 1000 -> (5, 10); 2000 -> (8, 30); 3000 -> (10, 50).
    .NET crosses 20 ms at exactly 1500; go tops out at 10 ms (no crossing).
    """
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate, (go_v, dn_v) in ((1000, (5.0, 10.0)), (2000, (8.0, 30.0)), (3000, (10.0, 50.0))):
        for block in (0, 1):
            for lang, val in (("go", go_v), ("dotnet", dn_v)):
                key = f"r{rate}.b{block}.{lang}"
                rec.probes.append(_done_probe(rate, block, lang))
                p99[key] = val
                under_warmed[key] = False
    rec.data = {"crossed": True, "bracket": [2000, 3000], "p99_ms": p99,
                "under_warmed": under_warmed, "rates": [1000, 2000, 3000]}
    return rec


def test_per_language_knees_are_fitted_alongside_the_pooled_one(isolated_results):
    run_id = "syn-split"
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_split_ladder_stage()))
    led.save()

    result = analyze.analyze_run(run_id)
    cell = result.cells[0]
    assert cell.knee.scope == "pooled"
    dn = cell.knee_by_lang["dotnet"]
    go = cell.knee_by_lang["go"]
    # .NET crosses 20 ms between (1000,10) and (2000,30) -> exactly 1500
    assert dn.crossed is True and abs(dn.knee_rps - 1500.0) < 1.0
    # go never reaches the SLO: reported as such, with the ladder's top rung
    assert go.crossed is False and go.knee_rps is None
    assert go.max_rung_rps == 3000.0 and abs(go.max_rung_p99_ms - 10.0) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "pooled (go+.NET)" in md
    assert "no crossing within the measured ladder (max rung 3000 rps" in md
    assert "not either language's knee" in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    by_lang = fits["cells"][0]["knee_by_language"]
    assert abs(by_lang["dotnet"]["knee_rps"] - 1500.0) < 1.0
    assert by_lang["go"]["knee_rps"] is None
    assert fits["cells"][0]["knee_pooled"]["scope"] == "pooled"


def test_pooled_knee_that_kept_one_language_is_not_called_an_ensemble_knee(isolated_results):
    """Under-warmed exclusion is language-asymmetric: when it empties one
    language out of every rung, the "pooled" fit IS the other language's knee.

    The report must say so; the blanket "this is not either language's knee"
    note is FALSE in that case, and it hides the more damaging fact that the
    cell's confirm rates were selected by a one-language fit.
    """
    run_id = "syn-onelang"
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(
        cell_id="headline.mixed.gc-generous", ladder=_ladder_stage(flagged_lang="go"),
    ))
    led.save()

    result = analyze.analyze_run(run_id)
    comp = result.cells[0].knee.composition
    assert comp["languages_present"] == ["dotnet"]
    assert comp["warm_probes"] == {"go": 0, "dotnet": 4}

    md = result.report_path.read_text(encoding="utf-8")
    assert "this fit IS .NET's knee" in md
    assert "pooled -> .NET probes only" in md
    # the false blanket claim must NOT be attached to THIS fit (the section prose
    # still explains both cases; it is the per-row note that must be true)
    assert "not either language's knee" not in (result.cells[0].knee.note or "")
    assert "were selected by a fit that saw one language only" in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert fits["cells"][0]["knee_pooled"]["composition"]["rungs_reached"]["go"] == 0


def test_pooled_knee_with_both_languages_still_says_it_is_neither_knee(isolated_results):
    """The converse: where both languages contribute warm probes, the pooled fit
    really is an ensemble crossing and the note must keep saying so."""
    _synthetic_ledger("syn-bothlang")
    result = analyze.analyze_run("syn-bothlang")
    comp = result.cells[0].knee.composition
    assert comp["languages_present"] == ["dotnet", "go"]
    md = result.report_path.read_text(encoding="utf-8")
    assert "not either language's knee" in md
    assert "this fit IS" not in md


def test_knee_interval_is_labelled_non_inferential_with_the_reason(isolated_results):
    """`ci_valid` is False for every ladder-stage fit (reps_per_rung < confirm_reps);
    the report must say what the printed interval IS instead of calling it a CI."""
    _synthetic_ledger("syn-ci")
    md = analyze.analyze_run("syn-ci").report_path.read_text(encoding="utf-8")
    assert "NOT a valid bootstrap CI here" in md
    assert "ladder.confirm_reps" in md
    assert "| 5% CI |" not in md  # the old mislabelled header must be gone


# --------------------------------------------------------------------------- #
# per-language p99 at the offered rate, and the paired verdict's rate label
# --------------------------------------------------------------------------- #
def _two_rate_confirm() -> StageRecord:
    """Confirm at TWO bracket rates: go meets the 20 ms SLO at both, .NET at
    neither, and .NET is worse at the higher rate."""
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    for rate, (go_v, dn_v) in ((1500, (10.0, 30.0)), (2000, (12.0, 50.0))):
        for block in range(3):
            for lang, val in (("go", go_v), ("dotnet", dn_v)):
                key = f"r{rate}.b{block}.{lang}"
                rec.probes.append(_done_probe(rate, block, lang))
                p99[key] = val + 0.1 * block      # non-zero variance for the t-CI
                under_warmed[key] = False
    rec.data = {"p99_ms": p99, "under_warmed": under_warmed, "confirm_rates": [1500, 2000]}
    return rec


def test_paired_verdicts_are_per_rate_and_carry_their_rate(isolated_results):
    run_id = "syn-rates"
    _ledger_with_confirm(run_id, _two_rate_confirm())
    result = analyze.analyze_run(run_id)
    cell = result.cells[0]

    assert [p.rate for p in cell.paired_by_rate] == [1500, 2000]
    assert all(p.n_blocks == 3 for p in cell.paired_by_rate)
    assert cell.paired.rate == 1500                      # primary = lowest rate
    assert all(p.ci_pct == 95.0 for p in cell.paired_by_rate)
    assert all(p.margin_pct == 5.0 for p in cell.paired_by_rate)

    md = result.report_path.read_text(encoding="utf-8")
    assert "95% CI (paired t)" in md                      # confidence level, not the margin
    assert "equiv. margin" in md
    assert "One verdict PER OFFERED RATE" in md

    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert [p["rate_rps"] for p in fits["cells"][0]["paired_by_rate"]] == [1500, 2000]


def test_cpu_per_request_is_request_weighted_and_states_its_denominator(isolated_results):
    """CPU-ms/request must be total CPU / total requests, not an unweighted mean
    of per-window ratios: otherwise the request count published beside it is
    not its denominator and the label is a lie.

    Two go windows: 1000 reqs at 0.1 CPU-ms/req and 9000 reqs at 0.2. The
    unweighted mean of the ratios is 0.15; the honest per-request cost over the
    aggregated windows is 1900 CPU-ms / 10000 requests = 0.19.
    """
    run_id = "syn-weighted"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    _write_probe_window(base / "block0", "go", reqs=1000, cpu_us=100_000,
                        anon=30_000_000, peak=60_000_000)
    _write_probe_window(base / "block1", "go", reqs=9000, cpu_us=1_800_000,
                        anon=30_000_000, peak=60_000_000)
    for block in range(2):
        _write_probe_window(base / f"block{block}", "dotnet", reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    go = result.cells[0].resource.go
    assert go.requests == 10_000
    assert abs(go.cpu_ms_total - 1900.0) < 1e-9
    assert abs(go.cpu_ms_per_req - 0.19) < 1e-9            # weighted, NOT 0.15
    assert abs(go.cpu_ms_per_req_unweighted - 0.15) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "THE AGGREGATION RULE" in md
    assert "REQUEST-WEIGHTED" in md
    assert "requests serviced (the CPU denominator)" in md
    # the old, wrong label must be gone
    assert "the denominator of CPU-ms/request): " not in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    res = fits["cells"][0]["resource"]
    assert res["go"]["cpu_ms_per_req_aggregation"].startswith("request-weighted")
    assert res["cpu_ratio_dotnet_over_go_unweighted"] is not None


def test_stage_mix_asymmetry_is_published_and_quantified(isolated_results):
    """Go keeps a soak window that .NET does not: the two published CPU numbers
    then aggregate different stage mixes, so the composition must be a row of
    the table and the alternative weighting's ratio must be printed."""
    run_id = "syn-stagemix"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    cellbase = isolated_results / run_id / "cells" / "headline.mixed.gc-default"
    for block in range(2):
        bd = cellbase / "confirm" / f"rate{rate}" / f"block{block}"
        _write_probe_window(bd, "go", reqs=1000, cpu_us=100_000,
                            anon=30_000_000, peak=60_000_000)
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000,
                            anon=150_000_000, peak=180_000_000)
    # a go-only soak window (5x the requests of a confirm window), no .NET twin
    _write_probe_window(cellbase / "soak" / f"rate{rate}" / "block0", "go",
                        reqs=5000, cpu_us=600_000, anon=30_000_000, peak=60_000_000)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.go.stage_windows == {"confirm": 2, "soak": 1}
    assert res.dotnet.stage_windows == {"confirm": 2}
    # weighted and unweighted disagree here, and BOTH are published
    assert res.cpu_ratio != res.cpu_ratio_unweighted

    md = result.report_path.read_text(encoding="utf-8")
    assert "Stage-mix asymmetry in headline.mixed.gc-default" in md
    assert "stage mix of the windows used" in md
    assert "2 confirm, 1 soak" in md
    assert "equal-weight-per-window alternative gives" in md


def test_slo_verdict_states_its_rule_and_publishes_the_per_window_count(isolated_results):
    """The `meets SLO?` column is a MEAN of per-window p99s. A language can pass
    it while a window misses badly, so the rule must be stated and the count of
    windows that individually met the SLO published beside the verdict."""
    run_id = "syn-slorule"
    rate = 1000
    rec = StageRecord(state=State.DONE)
    p99: dict[str, float] = {}
    under_warmed: dict[str, bool] = {}
    # go: 5, 5, 45 -> mean 18.33 (under 20) but only 2 of 3 windows are under it
    for block, go_v in enumerate((5.0, 5.0, 45.0)):
        for lang, val in (("go", go_v), ("dotnet", 30.0 + block)):
            key = f"r{rate}.b{block}.{lang}"
            rec.probes.append(_done_probe(rate, block, lang))
            p99[key] = val
            under_warmed[key] = False
    rec.data = {"p99_ms": p99, "under_warmed": under_warmed, "confirm_rates": [rate]}
    _ledger_with_confirm(run_id, rec)

    result = analyze.analyze_run(run_id)
    rows = {(s.rate, s.lang): s for s in result.cells[0].slo_at_rates}
    go = rows[(rate, "go")]
    assert go.meets_slo is True                  # mean rule
    assert go.n_windows_met_slo == 2 and go.n_used == 3
    assert abs(go.max_p99_ms - 45.0) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "THE VERDICT RULE" in md
    assert "meets 20 ms? (mean rule)" in md
    assert "windows under 20 ms" in md
    assert "| 2/3 |" in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    slo_rows = fits["cells"][0]["slo_at_rates"]
    assert all(r["meets_slo_rule"] == "mean of per-window p99 <= slo_ms" for r in slo_rows)


def test_slo_readout_names_which_language_met_the_promise(isolated_results):
    run_id = "syn-slo"
    _ledger_with_confirm(run_id, _two_rate_confirm())
    result = analyze.analyze_run(run_id)
    rows = {(s.rate, s.lang): s for s in result.cells[0].slo_at_rates}
    assert rows[(1500, "go")].meets_slo is True
    assert rows[(1500, "dotnet")].meets_slo is False
    assert rows[(2000, "dotnet")].meets_slo is False

    md = result.report_path.read_text(encoding="utf-8")
    assert "held to the same OFFERED RATE, never to the same latency promise" in md
    assert "meets 20 ms?" in md


# --------------------------------------------------------------------------- #
# CPU-ms/request vs offered rate (the rate-dependence artifact)
# --------------------------------------------------------------------------- #
def test_cpu_per_request_vs_rate_curve_and_cv(isolated_results):
    """.NET's per-request CPU falls with rate while go's is flat: the curve, the
    per-rung ratio and both CVs must be published, not just one operating point."""
    run_id = "syn-curve"
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_ladder_stage()))
    led.save()
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    # go 0.1 CPU-ms/req at both rungs; .NET 0.5 then 0.3 -> ratio 5.00x then 3.00x.
    for rate, dn_us in ((1000, 500_000), (2000, 300_000)):
        for block in (0, 1):
            bd = base / f"rate{rate}" / f"block{block}"
            _write_probe_window(bd, "go", reqs=1000, cpu_us=100_000,
                                anon=30_000_000, peak=60_000_000)
            _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=dn_us,
                                anon=150_000_000, peak=180_000_000)

    result = analyze.analyze_run(run_id)
    cvr = result.cells[0].cpu_vs_rate
    assert cvr.available is True
    assert [r.rate for r in cvr.rungs] == [1000, 2000]
    assert abs(cvr.rungs[0].ratio_dotnet_over_go - 5.0) < 1e-9
    assert abs(cvr.rungs[1].ratio_dotnet_over_go - 3.0) < 1e-9
    assert cvr.cv_go_pct == 0.0                      # flat go column
    assert cvr.cv_dotnet_pct > 20.0                  # .NET moves with rate
    assert abs(cvr.ratio_min - 3.0) < 1e-9 and abs(cvr.ratio_max - 5.0) < 1e-9

    md = result.report_path.read_text(encoding="utf-8")
    assert "CPU-ms per request vs offered rate" in md
    assert "Coefficient of variation across the 2 rungs" in md
    fits = json.loads(result.fits_path.read_text(encoding="utf-8"))
    assert len(fits["cells"][0]["cpu_vs_rate"]["rungs"]) == 2


def test_cpu_vs_rate_is_available_when_only_dotnet_has_ladder_windows(isolated_results):
    """Availability must be decided over BOTH languages: a cell with .NET ladder
    windows and no go ones is not "no cgroup summaries on disk"."""
    run_id = "syn-curve-dn"
    led = Ledger(run_id=run_id, config_hash="testhash", rng_seed=42)
    led.cells.append(CellRecord(cell_id="headline.mixed.gc-default", ladder=_ladder_stage()))
    led.save()
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "ladder"
    for rate in (1000, 2000):
        for block in (0, 1):
            _write_probe_window(base / f"rate{rate}" / f"block{block}", "dotnet",
                                reqs=1000, cpu_us=300_000, anon=150_000_000,
                                peak=180_000_000)

    cvr = analyze.analyze_run(run_id).cells[0].cpu_vs_rate
    assert cvr.available is True
    assert [r.rate for r in cvr.rungs] == [1000, 2000]
    assert all(r.go_cpu_ms_per_req is None for r in cvr.rungs)
    assert all(r.dotnet_cpu_ms_per_req is not None for r in cvr.rungs)


# --------------------------------------------------------------------------- #
# scope section: what this run does NOT establish
# --------------------------------------------------------------------------- #
def test_scope_section_reports_capacity_gate_failure_and_missing_gates(isolated_results):
    run_id = "syn-scope"
    led = _synthetic_ledger(run_id)
    led.mark_phase(
        "capacity_gate", State.DONE,
        verdict="db-imposed-shared-knee", pg_knee_rps=666.67,
        levers_fired=["write_share", "cache_hit_rate", "pg_cores"],
    )
    led.mark_phase(
        "validation", State.DONE,
        co={"deferred": "bench-tier"}, logging_sink={"deferred": "bench-tier"},
        conn_sweep={"deferred": "bench-tier"},
    )

    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "The capacity gate did NOT pass" in md
    assert "db-imposed-shared-knee" in md
    # A pre-descope ledger carrying {"deferred": ...} entries still surfaces them.
    assert "Validation checks recorded as deferred (since descoped)" in md
    assert "never ran" in md
    # declared in spec/slo.yaml but never evaluated by the runner. The set is
    # taken from the runner's canonical registry, not restated in analyze.py.
    from ledgerbench.warmup import IMPLEMENTED_GATES

    from ledgerbench.config import load_slo

    declared = set(load_slo().warmup.gates)
    for gate in declared - set(IMPLEMENTED_GATES):
        assert f"`{gate}`" in md
    assert "`ledgerbench.warmup.IMPLEMENTED_GATES`" in md
    # per_endpoint_min_calls is a separate spec key, not one of the declared
    # gates: it must not be smuggled into the implemented-gate arithmetic
    assert "per_endpoint_min_calls" not in IMPLEMENTED_GATES
    assert "plus the separate `per_endpoint_min_calls` spec key" in md
    assert "none of which is in the unimplemented list" in md
    # the working-set / cache-residency caveat
    assert "Effective working set" in md
    assert "`cache_stats` are null" in md


def test_scope_section_omits_capacity_bullet_when_the_gate_passed(isolated_results):
    """A passing gate must not be described as a failure (the bullet is derived
    from the recorded verdict, not asserted)."""
    run_id = "syn-scope-pass"
    led = _synthetic_ledger(run_id)
    led.mark_phase("capacity_gate", State.DONE, verdict="pass", pg_knee_rps=5000.0)
    md = analyze.analyze_run(run_id).report_path.read_text(encoding="utf-8")
    assert "The capacity gate did NOT pass" not in md


def test_resource_notes_split_throttle_and_oom_and_label_peak_memory(isolated_results):
    """The old report printed one `clean` flag for both counters and called
    memory.peak "peak RSS"; both are fixed and scoped to the aggregated windows."""
    run_id = "syn-notes"
    rate = 1000
    _ledger_with_confirm(run_id, _confirm_stage_at(rate, 2))
    base = isolated_results / run_id / "cells" / "headline.mixed.gc-default" / "confirm" / f"rate{rate}"
    for block in range(2):
        bd = base / f"block{block}"
        _write_probe_window(bd, "go", reqs=1000, cpu_us=100_000, anon=30_000_000,
                            peak=60_000_000)
        # .NET took a CFS throttle in one window: the two verdicts must diverge.
        _write_probe_window(bd, "dotnet", reqs=1000, cpu_us=300_000, anon=150_000_000,
                            peak=180_000_000, throttled=1 if block == 0 else 0)

    result = analyze.analyze_run(run_id)
    res = result.cells[0].resource
    assert res.go.throttle_clean is True
    assert res.dotnet.throttle_clean is False and res.dotnet.oom_clean is True

    md = result.report_path.read_text(encoding="utf-8")
    assert "cgroup memory.peak (MB, container lifetime)" in md
    assert "peak RSS" not in md
    assert "CPU throttle counters NON-ZERO in headline.mixed.gc-default/dotnet" in md
    assert "cgroup OOM events zero" in md
    assert "aggregated confirm + soak windows in the table above" in md
    # pool warmth prints total/max, not the vacuous total/total
    assert "open connections / configured max" in md
