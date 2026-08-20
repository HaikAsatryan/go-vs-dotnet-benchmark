"""Targets/body generator: mix, pages, payload sizes, determinism, sku, reports.

Implements the spec/workload.yaml assertions the runner must satisfy
(bench/NOTES.md ("Body sizing" and "Zipf")). Uses smoke scale so the Zipf tables build fast.
"""

from __future__ import annotations

import json
import re

import pytest

from ledgerbench import vegeta
from ledgerbench.config import load_workload
from ledgerbench.util.pcg64 import PCG64

SKU_RE = re.compile(r"^[A-Z]{2}-[0-9]{4}$")


@pytest.fixture(scope="module")
def wl():
    return load_workload()


@pytest.fixture(scope="module")
def generated(tmp_path_factory, wl):
    out = tmp_path_factory.mktemp("targets")
    return vegeta.generate_targets(wl, out_dir=out, count=8000, seed=12345, scale="smoke")


def test_mix_proportions_within_tolerance(generated, wl):
    total = sum(generated.endpoint_counts.values())
    assert total == 8000
    for name, weight in wl.mix.as_pairs():
        observed = generated.endpoint_counts[name] / total
        expected = weight / 100.0
        # 2 percentage points absolute tolerance at n=8000.
        assert abs(observed - expected) < 0.02, (name, observed, expected)


def test_create_body_sizes_within_tolerance(generated):
    spec_target = 2048
    tol = 512
    assert generated.create_body_bytes  # some were generated
    for b in generated.create_body_bytes:
        assert spec_target - tol <= b <= spec_target + tol, b


def test_quote_body_sizes_within_tolerance(generated):
    spec_target = 8192
    tol = 1024
    assert generated.quote_body_bytes
    for b in generated.quote_body_bytes:
        assert spec_target - tol <= b <= spec_target + tol, b


def test_targets_file_deterministic(tmp_path, wl):
    a = vegeta.generate_targets(wl, out_dir=tmp_path / "a", count=3000, seed=12345, scale="smoke")
    b = vegeta.generate_targets(wl, out_dir=tmp_path / "b", count=3000, seed=12345, scale="smoke")
    assert a.targets_path.read_bytes() == b.targets_path.read_bytes()
    assert a.endpoint_counts == b.endpoint_counts


def test_seed_changes_stream(tmp_path, wl):
    a = vegeta.generate_targets(wl, out_dir=tmp_path / "a", count=3000, seed=1, scale="smoke")
    b = vegeta.generate_targets(wl, out_dir=tmp_path / "b", count=3000, seed=2, scale="smoke")
    assert a.targets_path.read_bytes() != b.targets_path.read_bytes()


def test_sku_format(wl):
    rng = PCG64(5)
    cats = vegeta._sku_categories(wl.payloads.create_invoice.fields)
    for _ in range(5000):
        sku = vegeta.gen_sku(rng, cats)
        assert SKU_RE.match(sku), sku
        assert sku[:2] in cats


def test_quote_has_exactly_50_items(tmp_path, wl):
    rng = PCG64(11)
    body = vegeta.gen_quote_body(rng, wl)
    obj = json.loads(body)
    assert len(obj["items"]) == 50
    assert obj["currency"] == "USD"
    for it in obj["items"]:
        assert set(it.keys()) == {"sku", "qty", "unit_price_minor"}
        assert SKU_RE.match(it["sku"])
        assert 1 <= it["qty"] <= 200
        assert 100 <= it["unit_price_minor"] <= 1_000_000


def test_create_body_schema(tmp_path, wl):
    rng = PCG64(13)
    body = vegeta.gen_create_body(rng, wl, customer_id=42)
    obj = json.loads(body)
    assert obj["customer_id"] == 42
    assert obj["currency"] == "USD"
    assert obj["status"] in ("draft", "open")
    assert 3 <= len(obj["items"]) <= 8
    for it in obj["items"]:
        assert SKU_RE.match(it["sku"])
        assert 1 <= len(it["description"]) <= 200
        assert 1 <= it["qty"] <= 50
        assert 100 <= it["unit_price_minor"] <= 5_000_000


def test_page_distribution_skew(tmp_path, wl):
    # All list_invoices targets carry ?page=N; page 1 should dominate (~0.70).
    gt = vegeta.generate_targets(wl, out_dir=tmp_path, count=20000, seed=12345, scale="smoke")
    pages = []
    for line in gt.targets_path.read_text().splitlines():
        if line.startswith("GET ") and "/invoices?page=" in line:
            pages.append(int(line.rsplit("=", 1)[1]))
    assert pages
    frac1 = pages.count(1) / len(pages)
    assert abs(frac1 - 0.70) < 0.03
    assert all(1 <= p <= 5 for p in pages)


def test_targets_file_shape(generated):
    text = generated.targets_path.read_text()
    lines = text.splitlines()
    # Every GET line begins with a method token; body refs use @bodies/.
    methods = {ln.split(" ", 1)[0] for ln in lines if ln and not ln.startswith(("@", "Content-Type"))}
    assert methods <= {"GET", "POST"}
    assert any(ln.startswith("@bodies/") for ln in lines)


# --------------------------- report parsing -------------------------------- #
def test_vegeta_report_parsing():
    sample = {
        "requests": 6000,
        "rate": 199.8,
        "throughput": 199.5,
        "duration": 30_000_000_000,
        "success": 0.999,
        "status_codes": {"200": 5994, "500": 6},
        "latencies": {"mean": 1_000_000, "50th": 800_000, "99th": 5_000_000, "max": 9_000_000},
        "errors": [],
    }
    rep = vegeta.VegetaReport.from_json(json.dumps(sample))
    assert rep.requests == 6000
    assert rep.latency_p99_ns == 5_000_000
    assert abs(rep.error_rate - 0.001) < 1e-9
    assert rep.status_codes["500"] == 6


def test_achieved_vs_offered():
    rep = vegeta.VegetaReport(
        requests=100, rate=198.0, throughput=198.0, duration_ns=1, success_ratio=1.0,
        status_codes={}, latency_mean_ns=0, latency_p50_ns=0, latency_p99_ns=0,
        latency_max_ns=0, errors=[],
    )
    ok, ratio = vegeta.assert_achieved_vs_offered(rep, 200.0, tolerance_pct=2.0)
    assert ok and abs(ratio - 0.99) < 1e-9
    bad, ratio2 = vegeta.assert_achieved_vs_offered(rep, 250.0, tolerance_pct=2.0)
    assert not bad


def test_attack_argv_pins_params():
    params = vegeta.AttackParams(rate=4000, duration_s=120, max_workers=200, connections=200, timeout_s=5.0)
    argv = vegeta.build_attack_argv(
        targets_in_container="/io/targets.txt", out_bin_in_container="/io/out.bin", params=params
    )
    joined = " ".join(argv)
    assert "-rate=4000" in joined
    assert "-duration=120s" in joined
    assert "-keepalive=true" in joined
    assert "-timeout=5.0s" in joined
    assert "-output=/io/out.bin" in joined


def test_per_second_p99_buckets_by_integer_second():
    # three records in second 0, two in second 1 (RFC3339 + ns latency).
    ndjson = [
        '{"timestamp":"2026-06-06T01:30:00.100000000Z","latency":1000000}',
        '{"timestamp":"2026-06-06T01:30:00.500000000Z","latency":2000000}',
        '{"timestamp":"2026-06-06T01:30:00.900000000Z","latency":9000000}',
        '{"timestamp":"2026-06-06T01:30:01.100000000Z","latency":3000000}',
        '{"timestamp":"2026-06-06T01:30:01.900000000Z","latency":4000000}',
        "",  # blank line tolerated
    ]
    series = vegeta.per_second_p99_from_ndjson(iter(ndjson))
    secs = [s for s, _ in series]
    assert secs == [0, 1]
    # second 0 p99 (nearest-rank of [1,2,9] ms) -> the 9 ms tail
    assert abs(series[0][1] - 9.0) < 1e-6
    # second 1 p99 of [3,4] ms -> 4 ms
    assert abs(series[1][1] - 4.0) < 1e-6


def test_per_second_p99_csv_roundtrip(tmp_path):
    series = [(0, 9.0), (1, 4.0)]
    path = tmp_path / "p99.csv"
    vegeta.write_per_second_p99_csv(series, path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "second,p99_ms"
    assert lines[1].startswith("0,9.0")
    assert lines[2].startswith("1,4.0")


def test_per_second_p99_skips_malformed_lines():
    ndjson = ["not json", '{"latency":5}', '{"timestamp":"2026-06-06T01:30:00Z","latency":7000000}']
    series = vegeta.per_second_p99_from_ndjson(iter(ndjson))
    assert series == [(0, 7.0)]
