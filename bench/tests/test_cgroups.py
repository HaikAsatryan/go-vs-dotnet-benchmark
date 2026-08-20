"""cgroups sampler interface: Windows/non-Linux no-op."""

from __future__ import annotations

import sys

import pytest

from ledgerbench import cgroups
from ledgerbench.cgroups import CgroupSampler, Sample, WindowResult, disabled_result


def test_disabled_result_records_non_linux():
    r = disabled_result("ledgerline-sut-go-1")
    assert r.enabled is False
    assert r.note == "disabled (non-Linux)"
    assert r.container == "ledgerline-sut-go-1"


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux no-op behavior")
def test_sampler_is_noop_off_linux():
    s = CgroupSampler("ledgerline-sut-go-1")
    assert s.enabled is False
    s.start()  # must not raise, must not spawn anything
    result = s.stop()
    assert result.enabled is False
    assert result.note == "disabled (non-Linux)"
    assert result.samples == []


def test_window_result_csv_roundtrip(tmp_path):
    wr = WindowResult(
        container="c",
        enabled=True,
        samples=[
            Sample(monotonic_s=0.0, cpu_usage_usec=100, nr_throttled=0, throttled_usec=0, mem_anon=1024),
            Sample(monotonic_s=0.05, cpu_usage_usec=200, nr_throttled=0, throttled_usec=0, mem_anon=2048),
        ],
    )
    p = tmp_path / "cpu.csv"
    wr.write_csv(p)
    lines = p.read_text().strip().splitlines()
    assert lines[0] == "monotonic_s,cpu_usage_usec,nr_throttled,throttled_usec,mem_anon"
    assert lines[1].endswith(",100,0,0,1024")
    assert len(lines) == 3


def test_percentile_helper():
    # Nearest-rank p99 over 1..100: index round(0.99*99)=98 -> value 99.
    vals = list(range(1, 101))
    assert cgroups._percentile(vals, 0.99) == 99
    assert cgroups._percentile(vals, 1.0) == 100
    assert cgroups._percentile([], 0.99) is None
    assert cgroups._percentile([5], 0.5) == 5


def test_resolve_cgroup_path_none_off_linux():
    if not sys.platform.startswith("linux"):
        assert cgroups.resolve_cgroup_path("whatever", pid=1) is None
