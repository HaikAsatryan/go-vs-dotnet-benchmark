"""Markdown report assembly.

Pure functions that turn analysis outputs (paired verdicts, knee estimates,
GC/pool tables, the DB-CPU-per-request attribution) into markdown tables for
results/<run_id>/report/ and docs/ snippets. pandas assembles the frames;
No figures are produced; the caller gets markdown and the fits JSON.

The $/M illustrative cost uses an EXPLICIT formula with PINNED prices and a
sensitivity band so it is reproducible and clearly labeled illustrative, not a
vendor quote.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .paired import PairedResult

_VERDICT_WORD = {
    "tie": "tie (within +-margin)",
    "go_better": "Go better",
    "dotnet_better": ".NET better",
}


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def headline_ratio_table(results: list[PairedResult]) -> str:
    """One row per headline metric: means, paired diff %, CI, verdict, effect size.

    The interval column is headed by its CONFIDENCE LEVEL (`ci_pct`, 95%) and the
    pre-registered equivalence band (`margin_pct`, +-5%) is its own column. They
    are different numbers: heading the 95% interval with the margin (the old
    `f"{int(margin_pct)}% CI"`) rendered it as a "5% CI".
    """
    rows = []
    for r in results:
        rows.append(
            {
                "metric": r.metric,
                "Go": f"{r.mean_go:.4g}",
                ".NET": f"{r.mean_dotnet:.4g}",
                "diff %": _fmt_pct(r.mean_diff_pct),
                f"{r.ci_pct:g}% CI (paired t)": f"[{r.ci_low_pct:+.2f}, {r.ci_high_pct:+.2f}]",
                "equiv. margin": f"+-{r.margin_pct:g}%",
                "verdict": _VERDICT_WORD.get(r.verdict, r.verdict),
                "effect (d_z)": f"{r.effect_size:.2f}",
                "method": r.method,
            }
        )
    return _df_to_md(pd.DataFrame(rows))


def db_cpu_attribution_table(rows: list[dict]) -> str:
    """The novel DB-CPU-per-request column.

    Each row: {endpoint, go_db_cpu_ms_per_req, dotnet_db_cpu_ms_per_req,
    go_app_cpu_ms_per_req, dotnet_app_cpu_ms_per_req}. Attributes the PG CPU cost
    per request separately from app CPU so the driver/round-trip delta is visible.
    """
    df = pd.DataFrame(rows)
    return _df_to_md(df)


def gc_pool_table(rows: list[dict]) -> str:
    """Per-run GC + pool stats (.NET System.Runtime vs Go runtime/metrics).

    Each row mirrors /runtime-stats: {language, gen0, gen1, gen2,
    pause_total_seconds, heap_committed_bytes, allocated_total_bytes,
    db_pool_total, db_empty_acquire_count, redis_pool_total}.
    """
    return _df_to_md(pd.DataFrame(rows))


@dataclass(frozen=True)
class CostInputs:
    """Pinned inputs for the illustrative $/M-requests figure (fully disclosed)."""

    vcpu_hour_usd: float          # pinned cloud vCPU-hour price
    cpu_ms_per_req_go: float
    cpu_ms_per_req_dotnet: float
    sensitivity_pct: float = 20.0  # +-band on the price assumption


def cost_per_million(inputs: CostInputs) -> dict:
    """Illustrative $/M requests with the explicit formula + sensitivity band.

    formula:  $/M = (cpu_ms_per_req / 1000) * (vcpu_hour_usd / 3600) * 1e6
            = cpu_ms_per_req * vcpu_hour_usd / 3.6   (USD per million requests)
    """
    def usd_per_m(cpu_ms: float, price: float) -> float:
        return cpu_ms * price / 3.6

    band = inputs.sensitivity_pct / 100.0
    out = {}
    for lang, cpu in (("go", inputs.cpu_ms_per_req_go), ("dotnet", inputs.cpu_ms_per_req_dotnet)):
        mid = usd_per_m(cpu, inputs.vcpu_hour_usd)
        out[lang] = {
            "usd_per_million": mid,
            "low": usd_per_m(cpu, inputs.vcpu_hour_usd * (1 - band)),
            "high": usd_per_m(cpu, inputs.vcpu_hour_usd * (1 + band)),
        }
    out["formula"] = "usd_per_million = cpu_ms_per_req * vcpu_hour_usd / 3.6"
    out["vcpu_hour_usd"] = inputs.vcpu_hour_usd
    out["sensitivity_pct"] = inputs.sensitivity_pct
    return out


def cost_table(inputs: CostInputs) -> str:
    c = cost_per_million(inputs)
    rows = [
        {
            "language": lang,
            "$/M (mid)": f"${c[lang]['usd_per_million']:.2f}",
            f"+-{inputs.sensitivity_pct:g}% band": f"[${c[lang]['low']:.2f}, ${c[lang]['high']:.2f}]",
        }
        for lang in ("go", "dotnet")
    ]
    table = _df_to_md(pd.DataFrame(rows))
    note = (
        f"\n*Illustrative only.* `{c['formula']}`; "
        f"pinned vCPU-hour = ${inputs.vcpu_hour_usd:.4f}; "
        f"sensitivity band +-{inputs.sensitivity_pct:g}%.\n"
    )
    return table + note


def assemble_report(
    *,
    run_id: str,
    headline: list[PairedResult],
    db_cpu_rows: list[dict],
    gc_rows: list[dict],
    cost_inputs: CostInputs | None = None,
) -> str:
    """Top-level markdown document for results/<run_id>/report/report.md."""
    parts: list[str] = [f"# Ledgerline benchmark report - run `{run_id}`\n"]
    parts.append("## Headline metrics (paired, +-5% margin verdict)\n")
    parts.append(headline_ratio_table(headline))
    parts.append("\n## DB-CPU-per-request attribution\n")
    parts.append(db_cpu_attribution_table(db_cpu_rows))
    parts.append("\n## GC + pool stats\n")
    parts.append(gc_pool_table(gc_rows))
    if cost_inputs is not None:
        parts.append("\n## Illustrative cost per million requests\n")
        parts.append(cost_table(cost_inputs))
    return "\n".join(parts) + "\n"


def _df_to_md(df: pd.DataFrame) -> str:
    """DataFrame -> GitHub markdown table, without requiring the `tabulate` dep."""
    if df.empty:
        return "_(no data)_\n"
    cols = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"
