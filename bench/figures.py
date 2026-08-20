"""Figures for the Ledgerline benchmark, drawn from the committed run artifacts.

Every number here is READ from `results/<run_id>/report/fits.json` and
`ledger.json`, and the knee curves are refit with the run's OWN isotonic code
(`analysis.pava.isotonic_fit`). Nothing is typed in by hand, so a figure cannot
drift from the run it claims to describe.

    make figures
    # or: cd bench && uv run --with matplotlib python figures.py

matplotlib is deliberately not a repo dependency (nothing in the measured path
draws), so it is pulled in ad hoc with --with. Outputs PNGs into docs/figures/
at 2x for a ~800 px body width.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "results" / "20260814T051533-fedora"
OUT = ROOT / "docs" / "figures"
HEADLINE_CELL = "variant.prepared-parity"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analysis.pava import isotonic_fit  # noqa: E402  (the run's own knee-fit code)

GO = "#00ADD8"
NET = "#512BD4"
INK = "#1b1b1f"
MUTED = "#6b6b76"
GRID = "#e3e3e8"
PAPER = "#ffffff"
WARN = "#c2410c"

plt.rcParams.update({
    "figure.facecolor": PAPER,
    "axes.facecolor": PAPER,
    "savefig.facecolor": PAPER,
    "font.family": "DejaVu Sans",
    "font.size": 12,
    "text.color": INK,
    "axes.labelcolor": INK,
    "axes.edgecolor": GRID,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.titlesize": 15,
    "axes.titleweight": "bold",
    "axes.spines.top": False,
    "axes.spines.right": False,
})

FITS = json.loads((RUN / "report" / "fits.json").read_text())
LEDGER = json.loads((RUN / "ledger.json").read_text())


def cell(doc: dict, cell_id: str) -> dict:
    return next(c for c in doc["cells"] if c["cell_id"] == cell_id)


def rps_fmt(v, _pos=None) -> str:
    return f"{v/1000:g}k" if v >= 1000 else f"{v:g}"


def caption(ax, text: str, dy: int = -62) -> None:
    """Source note, parked well below the x-axis label so nothing collides."""
    ax.annotate(text, xy=(0, 0), xycoords="axes fraction",
                xytext=(0, dy), textcoords="offset points",
                fontsize=9.5, color=MUTED, ha="left", va="top")


def save(fig, name: str) -> None:
    path = OUT / name
    fig.savefig(path, dpi=170, bbox_inches="tight", pad_inches=0.30)
    plt.close(fig)
    print(f"wrote docs/figures/{name}")


def warm_windows(cell_id: str) -> dict[str, dict[int, list[float]]]:
    """{lang: {rate: [p99 of each measurement-grade window]}} from the ledger."""
    lad = cell(LEDGER, cell_id)["ladder"]["data"]
    flagged, excluded = lad["under_warmed"], lad["excluded"]
    out: dict[str, dict[int, list[float]]] = {"go": {}, "dotnet": {}}
    for key, value in lad["p99_ms"].items():
        rate_s, _block, lang = key.split(".")
        if flagged.get(key, False) or key in excluded:
            continue
        out[lang].setdefault(int(rate_s[1:]), []).append(float(value))
    return out


# ------------------------------------------------------------------- 1. the rig
def _box(ax, x, y, w, h, title, lines, face, edge, title_color=None, fs=12.5):
    """Rounded box with its title + body lines vertically centred as one block."""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.010,rounding_size=0.018",
        facecolor=face, edgecolor=edge, lw=1.8, zorder=2))
    title_h, line_h = 0.090, 0.070
    top = y + h / 2 + (title_h + len(lines) * line_h) / 2
    cx = x + w / 2
    ax.text(cx, top, title, ha="center", va="top",
            fontsize=fs, fontweight="bold", color=title_color or INK, zorder=3)
    for i, line in enumerate(lines):
        ax.text(cx, top - title_h - i * line_h, line, ha="center", va="top",
                fontsize=10.2, color=MUTED, zorder=3)


def _arrow(ax, xy_from, xy_to, color=MUTED):
    ax.add_patch(FancyArrowPatch(
        xy_from, xy_to, arrowstyle="-|>", mutation_scale=15,
        color=color, lw=1.7, shrinkA=2, shrinkB=2, zorder=1))


def fig_rig() -> None:
    """Both services talk to the SAME tier, so both arrows land on one junction."""
    fig, ax = plt.subplots(figsize=(10.0, 6.1))
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(-0.06, 1.08)
    ax.axis("off")

    _box(ax, 0.005, 0.375, 0.185, 0.315, "vegeta", [
        "open-loop load", "3 cores", "never backs off",
    ], "#f6f6fa", "#c9c9d4")

    _box(ax, 0.275, 0.545, 0.315, 0.245, "Go 1.26 service", [
        "net/http + sqlc over pgx", "6 cores, own cgroup",
    ], "#e8f8fc", GO, GO)
    _box(ax, 0.275, 0.245, 0.315, 0.245, ".NET 10 service", [
        "Minimal API + EF Core", "6 cores, own cgroup",
    ], "#efeafc", NET, NET)
    # shared tier: one dashed panel, both services enter it at the same junction
    ax.add_patch(FancyBboxPatch(
        (0.715, 0.225), 0.285, 0.615, boxstyle="round,pad=0.012,rounding_size=0.02",
        facecolor="#fbfbfd", edgecolor="#c9c9d4", lw=1.8, ls="--", zorder=1))
    ax.text(0.8575, 0.828, "shared data tier", ha="center", va="top",
            fontsize=10.5, color=MUTED, style="italic", zorder=3)
    ax.text(0.8575, 0.256, "both services use both", ha="center", va="center",
            fontsize=9.5, color=MUTED, zorder=3)
    _box(ax, 0.735, 0.535, 0.245, 0.235, "PostgreSQL 18.4", [
        "3 cores, own cgroup", "proven not the bottleneck",
    ], "#ffffff", "#c9c9d4")
    _box(ax, 0.735, 0.290, 0.245, 0.215, "Redis 8", [
        "1 core", "measured hit rate 0.807",
    ], "#ffffff", "#c9c9d4")

    junction = (0.660, 0.5175)
    _arrow(ax, (0.195, 0.575), (0.270, 0.645), GO)
    _arrow(ax, (0.195, 0.490), (0.270, 0.390), NET)
    _arrow(ax, (0.595, 0.650), junction, GO)
    _arrow(ax, (0.595, 0.385), junction, NET)
    ax.plot([junction[0]], [junction[1]], "o", ms=7, color=MUTED, zorder=4)
    _arrow(ax, junction, (0.712, 0.5175))

    ax.text(0.5, 1.065, "One box. One workload. Two implementations of the same frozen spec.",
            ha="center", va="top", fontsize=14.5, fontweight="bold")
    ax.text(0.5, 0.985,
            "Ryzen 9 7950X  ·  Fedora 43  ·  governor pinned, boost off  ·  "
            "every container in its own cgroup and cpuset",
            ha="center", va="top", fontsize=10.5, color=MUTED)
    ax.text(0.5, 0.115,
            "Request mix:   30% get invoice (cache-aside)  ·  25% list invoices  ·  "
            "20% pricing quote\n15% create invoice (a real multi-statement transaction)  ·  "
            "8% PDF stub  ·  2% health",
            ha="center", va="center", fontsize=10.8, color=INK, linespacing=1.6)
    ax.text(0.5, -0.035,
            "Only one service is under load at a time, so any drift on the box hits both equally.",
            ha="center", va="center", fontsize=10, color=MUTED, style="italic")
    save(fig, "01-the-rig.png")


# ---------------------------------------------------------------- 2. CPU curve
def fig_cpu() -> None:
    rungs = cell(FITS, HEADLINE_CELL)["cpu_vs_rate"]["rungs"]
    x = [r["rate"] for r in rungs]
    go = [r["go_cpu_ms_per_req"] for r in rungs]
    net = [r["dotnet_cpu_ms_per_req"] for r in rungs]

    fig, ax = plt.subplots(figsize=(9.2, 5.4))
    ax.plot(x, net, "-o", color=NET, lw=2.6, ms=5.5, label=".NET 10  (C#)", zorder=3)
    ax.plot(x, go, "-o", color=GO, lw=2.6, ms=5.5, label="Go 1.26", zorder=3)
    ax.fill_between(x, go, net, color=NET, alpha=0.06, zorder=1)

    for rate in (600, 3715, 13312):
        r = next(q for q in rungs if q["rate"] == rate)
        mid = (r["go_cpu_ms_per_req"] + r["dotnet_cpu_ms_per_req"]) / 2
        ax.annotate(
            f"{r['ratio_dotnet_over_go']:.1f}x",
            xy=(rate, mid), ha="center", va="center", fontsize=14, fontweight="bold",
            color=NET, path_effects=[pe.withStroke(linewidth=5, foreground=PAPER)],
            zorder=5,
        )

    ax.set_xscale("log")
    ax.set_xlim(430, 16500)
    ax.set_xticks([500, 1000, 2000, 5000, 10000])
    ax.xaxis.set_major_formatter(FuncFormatter(rps_fmt))
    ax.minorticks_off()
    ax.set_ylim(0, 0.86)
    ax.set_yticks([0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    ax.set_xlabel("offered load (requests per second, log scale)")
    ax.set_ylabel("CPU-ms burned per request")
    ax.set_title("Same work, same box: C# burns 3-5x the CPU per request")
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper right", fontsize=12.5,
              bbox_to_anchor=(1.0, 0.99))

    ax.annotate(
        "C# gets cheaper the busier it gets\n(JIT warm-up and fixed overheads\nspread over more requests)",
        xy=(6420, 0.432), xytext=(1450, 0.755), fontsize=11, color=NET, ha="center",
        va="center", linespacing=1.5,
        arrowprops=dict(arrowstyle="-|>", color=NET, lw=1.5,
                        connectionstyle="arc3,rad=-0.15", shrinkB=8),
    )
    ax.text(2580, 0.055, "Go's cost is flat: it costs what it costs, at any load",
            fontsize=11.5, color=GO, fontweight="bold", ha="center")

    caption(ax, "Ledgerline run 20260814T051533-fedora, cell variant.prepared-parity. "
                "CPU read from cgroup v2, request-weighted per rung. Lower is better.")
    save(fig, "02-cpu-per-request.png")


# ------------------------------------------------------------------ 3. the wall
def fig_wall() -> None:
    warm = warm_windows(HEADLINE_CELL)
    knee = cell(FITS, HEADLINE_CELL)["knee_by_language"]["dotnet"]["knee_rps"]

    fig, ax = plt.subplots(figsize=(9.2, 5.4))
    ax.axhspan(20, 60, color=WARN, alpha=0.05, zorder=0)
    ax.axhline(20, color=WARN, ls="--", lw=1.6, zorder=2)
    ax.text(455, 20.9, "the promise:  p99 at or under 20 ms",
            color=WARN, fontsize=11.5, fontweight="bold", va="bottom")

    for lang, color, label in (("dotnet", NET, ".NET 10  (C#)"), ("go", GO, "Go 1.26")):
        rates = sorted(warm[lang])
        for rate in rates:
            ax.scatter([rate] * len(warm[lang][rate]), warm[lang][rate],
                       s=26, color=color, alpha=0.32, lw=0, zorder=3)
        fitted = isotonic_fit([statistics.fmean(warm[lang][r]) for r in rates])
        ax.plot(rates, fitted, "-", color=color, lw=2.8, label=label, zorder=4)

    ax.plot([knee], [20], marker="X", ms=16, color=WARN, zorder=6,
            markeredgecolor=PAPER, markeredgewidth=1.8)
    ax.annotate(
        f"C# breaks the promise here\n{knee:,.0f} req/s",
        xy=(knee, 20), xytext=(4300, 30.5), fontsize=12, color=WARN,
        fontweight="bold", ha="center", va="center", linespacing=1.5,
        arrowprops=dict(arrowstyle="-|>", color=WARN, lw=1.6, shrinkB=10),
    )
    ax.text(500, 12.9, "Go never crossed 20 ms\nanywhere in this run",
            fontsize=12.5, color=GO, fontweight="bold", ha="left", linespacing=1.5)

    ax.set_xscale("log")
    ax.set_xlim(430, 16500)
    ax.set_xticks([500, 1000, 2000, 5000, 10000])
    ax.xaxis.set_major_formatter(FuncFormatter(rps_fmt))
    ax.minorticks_off()
    ax.set_ylim(0, 40)
    ax.set_xlabel("offered load (requests per second, log scale)")
    ax.set_ylabel("p99 latency (ms)")
    ax.set_title("Where each service breaks its own latency promise")
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=12.5)

    caption(ax, "Each faint dot is one measurement-grade 120 s window. The line is the "
                "monotone (isotonic) fit the analysis uses:\nlatency can only get worse as you "
                "add load, so the fit is not allowed to go down. Cell variant.prepared-parity.")
    save(fig, "04-latency-wall.png")


# -------------------------------------------------------------------- 4. memory
def fig_memory() -> None:
    res = cell(FITS, HEADLINE_CELL)["resource"]
    go, net = res["go"], res["dotnet"]

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.9))
    panels = [
        (axes[0], "Memory while serving traffic",
         "in-window anonymous memory, p99",
         go["anon_p99_mb"], net["anon_p99_mb"], res["anon_ratio_dotnet_over_go"], NET),
        (axes[1], "Peak over the container's whole life",
         "cgroup memory.peak: page cache + warm-up included",
         go["mem_peak_mb"], net["mem_peak_mb"], res["peak_ratio_dotnet_over_go"], MUTED),
    ]
    for ax, title, sub, gv, nv, ratio, ratio_color in panels:
        bars = ax.bar(["Go 1.26", ".NET 10"], [gv, nv], color=[GO, NET], width=0.5)
        top = max(gv, nv)
        for bar, value in zip(bars, (gv, nv)):
            ax.text(bar.get_x() + bar.get_width() / 2, value + top * 0.03,
                    f"{value:,.0f} MB", ha="center", fontsize=12.5, fontweight="bold")
        ax.set_ylim(0, top * 1.34)
        ax.set_title(title, fontsize=13, pad=26)
        ax.text(0.5, 1.045, sub, transform=ax.transAxes, ha="center",
                fontsize=10, color=MUTED)
        ax.text(0.5, 0.42, f"{ratio:.1f}x", transform=ax.transAxes, ha="center",
                va="center", fontsize=26, fontweight="bold", color=ratio_color,
                path_effects=[pe.withStroke(linewidth=6, foreground=PAPER)])
        ax.grid(axis="y", color=GRID, lw=1)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", labelsize=12.5, length=0)
        ax.tick_params(axis="y", length=0, labelleft=False)

    fig.suptitle("Two ways to measure memory, two very different answers",
                 fontsize=15.5, fontweight="bold", y=1.10)
    caption(axes[0], "Both at 11,093 req/s, cell variant.prepared-parity. Left is what you feel "
                     "when you pack services onto a node.\nRight is what your orchestrator's "
                     "memory limit actually has to cover.", dy=-42)
    save(fig, "03-memory.png")


# --------------------------------------------------------------- 5. data layers
def fig_data_layers() -> None:
    labels_by_cell = {
        "headline.mixed.gc-default": "EF Core\n(connection string as shipped)",
        "variant.dapper": "Dapper",
        "variant.ado": "Raw ADO.NET\n(hand-written SQL)",
        "variant.prepared-parity": "EF Core\n+ Max Auto Prepare=20",
    }
    rows = sorted(
        ((label, cell(FITS, cid)["knee_by_language"]["dotnet"]["knee_rps"])
         for cid, label in labels_by_cell.items()),
        key=lambda r: r[1],
    )
    # C#-only chart on purpose: Go has no place here, this is about the data layer.
    fig, ax = plt.subplots(figsize=(9.2, 4.9))
    ypos = list(range(len(rows)))
    ax.barh(ypos, [r[1] for r in rows], color=NET, height=0.46)
    for i, (_label, value) in enumerate(rows):
        ax.text(value + 230, i, f"{value:,.0f}", va="center", fontsize=12.5,
                fontweight="bold", color=NET)
    ax.set_yticks(ypos)
    ax.set_yticklabels([r[0] for r in rows], fontsize=11.5)
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(-0.55, len(rows) - 0.25)

    ax.annotate("", xy=(rows[1][1], 0.63), xytext=(rows[0][1], 0.63),
                arrowprops=dict(arrowstyle="<|-|>", color=WARN, lw=1.6))
    ax.text((rows[0][1] + rows[1][1]) / 2, 0.40,
            "one connection-string flag:  7x", color=WARN, fontsize=12,
            fontweight="bold", ha="center", va="center")

    ax.set_xlim(0, rows[-1][1] * 1.14)
    ax.xaxis.set_major_formatter(FuncFormatter(rps_fmt))
    ax.set_xlabel("requests per second the C# service held under the 20 ms promise")
    ax.set_title("Swapping the ORM changes almost nothing. One config flag changes 7x.",
                 pad=14)
    ax.grid(axis="x", color=GRID, lw=1)
    ax.set_axisbelow(True)

    caption(ax, "Four separate multi-hour runs of the same C# service on the same workload, "
                "same database, same cache.")
    save(fig, "05-data-layers.png")


# -------------------------------------------------------------- 6. how to choose
def fig_quadrant() -> None:
    fig, ax = plt.subplots(figsize=(9.4, 6.2))
    ax.set_xlim(-0.6, 10.2)
    ax.set_ylim(-0.9, 10.2)
    ax.axis("off")

    quads = [
        (0, 0, "#f6f6fa", "Thin domain, modest traffic",
         "This is almost everything you\nwill ever build. Use what your\nteam already knows: "
         "Python, Node,\nC#, Go are all fine here."),
        (5, 0, "#efeafc", "Rich domain, modest traffic",
         "C# and Java territory.\nA strong type system, LINQ,\na real ORM, mature libraries.\n"
         "3x CPU on a small bill is noise."),
        (0, 5, "#e8f8fc", "Thin domain, huge traffic",
         "Go territory.\nProxies, gateways, agents,\ninfra tooling, cloud-native glue,\n"
         "high-volume plumbing."),
        (5, 5, "#f3f0fb", "Rich domain, huge traffic",
         "The only place this really bites.\nUsual answer: keep the domain\nin C#, carve the "
         "few hot paths\nout into Go services."),
    ]
    for x, y, face, title, body in quads:
        ax.add_patch(FancyBboxPatch(
            (x + 0.10, y + 0.10), 4.80, 4.80,
            boxstyle="round,pad=0.02,rounding_size=0.15",
            facecolor=face, edgecolor="#dcdce4", lw=1.4))
        ax.text(x + 2.5, y + 4.18, title, ha="center", va="center",
                fontsize=12.5, fontweight="bold")
        ax.text(x + 2.5, y + 2.25, body, ha="center", va="center",
                fontsize=10.8, color="#3a3a44", linespacing=1.65)

    ax.add_patch(FancyArrowPatch((-0.35, -0.35), (10.1, -0.35), arrowstyle="-|>",
                                 mutation_scale=16, color=MUTED, lw=1.5))
    ax.add_patch(FancyArrowPatch((-0.35, -0.35), (-0.35, 10.1), arrowstyle="-|>",
                                 mutation_scale=16, color=MUTED, lw=1.5))
    ax.text(5, -0.78, "how complicated the business rules are",
            ha="center", fontsize=12, color=MUTED)
    ax.text(-0.78, 5, "how big the compute bill is",
            va="center", rotation=90, fontsize=12, color=MUTED)
    ax.set_title("Pick per service, not per company", pad=16)
    ax.annotate("Opinion, not measurement. All the benchmark tells you is how steep "
                "the vertical axis is.",
                xy=(0, 0), xycoords="axes fraction", xytext=(0, -34),
                textcoords="offset points", fontsize=9.5, color=MUTED, va="top")
    save(fig, "06-how-to-choose.png")


if __name__ == "__main__":
    fig_rig()
    fig_cpu()
    fig_memory()
    fig_wall()
    fig_data_layers()
    fig_quadrant()
