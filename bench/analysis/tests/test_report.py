"""Smoke tests: report emits valid markdown."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis import report
from analysis.paired import paired_difference


def test_report_headline_table_markdown():
    rng = np.random.default_rng(1)
    dn = 100 + rng.normal(0, 1, 12)
    go = 80 + rng.normal(0, 1, 12)
    r = paired_difference(go, dn, metric="cpu_ms_per_req")
    md = report.headline_ratio_table([r])
    assert md.startswith("| metric")
    assert "cpu_ms_per_req" in md
    assert "Go better" in md


def test_cost_per_million_formula():
    inputs = report.CostInputs(vcpu_hour_usd=0.04, cpu_ms_per_req_go=0.5,
                               cpu_ms_per_req_dotnet=0.8, sensitivity_pct=20.0)
    c = report.cost_per_million(inputs)
    # 0.5 ms * $0.04 / 3.6 = $0.005555.. per million
    assert abs(c["go"]["usd_per_million"] - (0.5 * 0.04 / 3.6)) < 1e-12
    assert c["go"]["low"] < c["go"]["usd_per_million"] < c["go"]["high"]


def test_assemble_report_runs():
    rng = np.random.default_rng(2)
    r = paired_difference(90 + rng.normal(0, 1, 10), 100 + rng.normal(0, 1, 10),
                          metric="knee_rps", lower_is_better=False)
    md = report.assemble_report(
        run_id="t1",
        headline=[r],
        db_cpu_rows=[{"endpoint": "GET /invoices/{id}", "go_db_cpu_ms_per_req": 0.2,
                      "dotnet_db_cpu_ms_per_req": 0.25}],
        gc_rows=[{"language": "go", "gen2": 10}, {"language": "dotnet", "gen0": 100}],
        cost_inputs=report.CostInputs(0.04, 0.5, 0.8),
    )
    assert "# Ledgerline benchmark report" in md
    assert "DB-CPU-per-request" in md
    assert "Illustrative cost" in md


def test_df_to_md_empty():
    assert "no data" in report._df_to_md(pd.DataFrame())
