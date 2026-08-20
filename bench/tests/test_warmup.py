"""Warmup gate predicates (spec/slo.yaml warmup)."""

from __future__ import annotations

from ledgerbench.config import load_slo
from ledgerbench.runtimestats import DbPool, Gc, GcCollections, RedisPool, RuntimeStats, Snapshot
from ledgerbench.warmup import (
    FLATNESS_TRAILING_SAMPLES,
    IMPLEMENTED_GATES,
    STATUS_NOT_IMPLEMENTED,
    WarmupState,
    endpoint_calls_met,
    gc_steady_dotnet,
    gc_steady_go,
    no_positive_trend,
    pools_warm,
)


def _stats(*, db_total=24, db_max=24, redis_total=4, calls=None, committed=0, gc_total=0) -> RuntimeStats:
    return RuntimeStats(
        db_pool=DbPool(max=db_max, total=db_total),
        redis_pool=RedisPool(max=16, total=redis_total),
        gc=Gc(heap_committed_bytes=committed, collections=GcCollections(gen2=gc_total)),
        endpoint_calls=calls or {},
    )


def test_no_positive_trend_flat():
    assert no_positive_trend([10, 10, 10, 10])
    assert no_positive_trend([10, 9, 10, 9, 8])  # declining/jitter


def test_no_positive_trend_rising_fails():
    assert not no_positive_trend([1, 2, 3, 4, 5, 6])


def test_no_positive_trend_short_series_passes():
    assert no_positive_trend([1])
    assert no_positive_trend([1, 2])


def test_pools_warm_requires_total_eq_max():
    assert pools_warm(_stats(db_total=24, db_max=24, redis_total=2))
    assert not pools_warm(_stats(db_total=20, db_max=24, redis_total=2))
    assert not pools_warm(_stats(db_total=24, db_max=24, redis_total=0))


def test_endpoint_calls_gate():
    mins = {"get_invoice": 100, "healthz": 10}
    s = _stats(calls={"GET /invoices/{id}": 150, "GET /healthz": 5})
    ok, shortfall = endpoint_calls_met(s, mins)
    assert not ok
    assert shortfall == {"GET /healthz": 5}
    s2 = _stats(calls={"GET /invoices/{id}": 150, "GET /healthz": 20})
    ok2, shortfall2 = endpoint_calls_met(s2, mins)
    assert ok2 and shortfall2 == {}


def test_warmup_state_all_pass():
    slo = load_slo()
    cfg = slo.warmup
    # shrink the per-endpoint minimums so the synthetic snapshot can satisfy them
    cfg = cfg.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, language="go", rate=500)
    calls = {k: 999999 for k in [
        "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
        "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz",
    ]}
    # feed several flat snapshots
    for i in range(6):
        s = _stats(calls=calls, committed=1_000_000, gc_total=i)  # gc cadence flat (+1/tick)
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0, p999_ms=8.0)
    gates = {g.name: g.passed for g in st.evaluate()}
    assert gates["per_endpoint_min_calls"]
    assert gates["pools_warm"]
    assert gates["p99_flat"]
    assert gates["p999_flat"]
    assert gates["gc_steady"]
    assert st.all_pass()


def test_warmup_state_detects_rising_p99():
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, language="dotnet", rate=500)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000 + i * 50_000)  # committed rising
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0 + i, p999_ms=8.0 + i)  # rising latency
    gates = {g.name: g.passed for g in st.evaluate()}
    assert not gates["p99_flat"]
    assert not gates["gc_steady"]  # committed climbing -> not steady
    assert not st.all_pass()


def test_gc_steady_symmetric_cadence_and_committed():
    # Both languages: cadence flat AND committed flat => steady.
    flat_committed = [1_000_000, 1_000_000, 1_000_000, 1_000_000]
    flat_cadence = [1, 1, 1, 1]
    assert gc_steady_go(flat_committed, flat_cadence)
    assert gc_steady_dotnet(flat_committed, flat_cadence)
    # Climbing committed with flat cadence => NOT steady, for BOTH languages.
    rising_committed = [1_000_000, 1_100_000, 1_200_000, 1_300_000]
    assert not gc_steady_go(rising_committed, flat_cadence)
    assert not gc_steady_dotnet(rising_committed, flat_cadence)
    # Flat committed with accelerating cadence => NOT steady, for BOTH languages.
    rising_cadence = [1, 2, 3, 4]
    assert not gc_steady_go(flat_committed, rising_cadence)
    assert not gc_steady_dotnet(flat_committed, rising_cadence)


def test_warmup_go_fails_gc_steady_on_climbing_heap():
    # Go probe: GC cadence flat but heap_committed still climbing must NOT pass.
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, language="go", rate=500)
    calls = {k: 999999 for k in [
        "GET /invoices/{id}", "GET /customers/{id}/invoices", "POST /pricing/quote",
        "POST /invoices", "POST /invoices/{id}/pdf-stub", "GET /healthz",
    ]}
    for i in range(6):
        # gc cadence flat (+1/tick) but committed heap climbing 50 KB/tick
        s = _stats(calls=calls, committed=1_000_000 + i * 50_000, gc_total=i)
        snap = Snapshot(captured_utc="t", monotonic_s=float(i), stats=s)
        st.observe(snap, p99_ms=5.0, p999_ms=8.0)
    gates = {g.name: g.passed for g in st.evaluate()}
    assert not gates["gc_steady"]  # climbing Go heap -> not steady
    assert not st.all_pass()


# --------------------------------------------------------------------------- #
# F9: gates spec/slo.yaml declares but this runner does not implement are
# recorded explicitly and never counted as passed (nor as failures).
# --------------------------------------------------------------------------- #
def _flat_state(language="go"):
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, language=language, rate=500)
    for i in range(6):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0, p999_ms=8.0)
    return st


def test_declared_but_unimplemented_gates_are_recorded():
    st = _flat_state()
    gates = {g.name: g for g in st.evaluate()}
    # spec/slo.yaml declares these two; warmup.py implements neither
    for name in ("hot_set_touched", "major_faults_asserted_equal"):
        assert name in gates, f"{name} must be visible in the probe record"
        assert gates[name].status == STATUS_NOT_IMPLEMENTED
        assert gates[name].passed is False       # never readable as a pass
        assert gates[name].enforced is False     # never decides under-warmed


def test_unimplemented_gates_do_not_flag_a_warm_probe():
    st = _flat_state()
    # every IMPLEMENTED gate passes, so the probe is warm despite the two
    # not_implemented records (otherwise every probe would be under-warmed).
    assert st.all_pass() is True
    assert {g.name for g in st.unimplemented_gates()} == {
        "hot_set_touched", "major_faults_asserted_equal"}


def test_implemented_gate_set_matches_what_evaluate_computes():
    st = _flat_state()
    measured = {g.name for g in st.evaluate() if g.enforced}
    assert measured == IMPLEMENTED_GATES | {"per_endpoint_min_calls"}


# --------------------------------------------------------------------------- #
# flatness window: the old trailing-5 window could only fail on a strictly
# monotone 5-point run.
# --------------------------------------------------------------------------- #
def test_flatness_window_is_wider_than_five_samples():
    assert FLATNESS_TRAILING_SAMPLES >= 30


def test_flatness_catches_a_slow_rise_that_trailing_five_would_miss():
    slo = load_slo()
    cfg = slo.warmup.model_copy(update={"per_endpoint_min_calls": {"healthz": 1}})
    st = WarmupState(cfg=cfg, language="go", rate=500)
    # 30 samples rising with a noisy wobble: the last 5 are NOT monotone, so the
    # old window could never fail; over the full window the trend is significant.
    for i in range(30):
        s = _stats(calls={"GET /healthz": 10}, committed=1_000_000, gc_total=i)
        wobble = 0.8 if i % 2 else 0.0
        st.observe(Snapshot(captured_utc="t", monotonic_s=float(i), stats=s),
                   p99_ms=5.0 + i * 0.5 - wobble, p999_ms=8.0)
    assert no_positive_trend(st.p99_series[-5:]) is True      # trailing 5: "flat"
    gates = {g.name: g.passed for g in st.evaluate()}
    assert gates["p99_flat"] is False                          # 30-sample window sees it
