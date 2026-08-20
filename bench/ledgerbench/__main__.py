"""ledgerbench CLI (typer). Subcommands: doctor smoke run-all gate ladder confirm
soak analyze status.

doctor + smoke run live. run-all is the live, ledger-backed, resumable
runner that orchestrates the phases (phases.py). gate/ladder/confirm/soak are
per-phase re-run entry
points wired to the same phase functions, each requiring --run-id. analyze reads
a finished ledger and emits the knee fits / stats / report. status renders the
ledger. (Seeding is compose/Make only; there is no `seed` subcommand here.)

A measured stage REFUSES TO RUN unless the capacity gate passed in that run;
that includes the per-stage commands, which re-assert the recorded verdict
before their first probe. The two operator escape hatches are environment
variables (see ENV_HELP below), echoed in `--help` so they are discoverable
without reading the source.

Everything is `uv run`-able directly so PowerShell users skip make.
"""

from __future__ import annotations

import platform
import sys

import typer
from rich.console import Console
from rich.table import Table

from . import doctor, phases, smoke
from .ledger import PHASE_ORDER, Ledger, State
from .util.clock import rfc3339_micros

# Operator environment variables. Repeated in the epilog of every command that
# can hit the capacity gate, so `--help` alone tells the operator what the two
# escape hatches are and what they cost.
ENV_HELP = (
    "Environment:\n"
    f"  {phases.ALLOW_DB_IMPOSED_KNEE_ENV}=1  proceed even though the capacity "
    "gate did not pass (default: ABORT before any measured probe). The run is "
    "stamped degraded in the ledger and the manifest, and every stage command "
    "re-checks the recorded verdict, so the caveat cannot be lost on a resume.\n"
    f"  {phases.CACHE_TTL_OVERRIDE_ENV}=<seconds>  supply the cache TTL that "
    "realizes the gate's cache_hit_rate lever (spec/slo.yaml pre-registers no "
    "value; without it the lever is recorded NOT APPLIED and a PASS that "
    "depends on it fails the gate).\n"
    f"  {phases.TARGET_COUNT_ENV}=<n>  requests in the generated --scale full "
    "targets file: the working-set dial (default 1000000, what the published run "
    "used). Lower it and the read path becomes cache-resident, which changes what "
    "the run measures. Pinned into the ledger at run start, so set it BEFORE "
    "starting a run; on resume the pin wins over the environment.\n"
    f"  {phases.RUN_VARIANTS_ENV}=<ids>  comma-separated variant cell ids to "
    "EXECUTE (e.g. variant.ado,variant.dapper); unknown ids abort. Default: "
    "variants are declared but not run. Each executed variant is a FULL cell "
    "(ladder/fit/confirm/soak, roughly 15 h at full scale). Also pinned into the "
    "ledger at run start."
)

app = typer.Typer(
    name="ledgerbench",
    help="Go vs .NET Ledgerline benchmark runner.",
    epilog=ENV_HELP,
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _new_run_id() -> str:
    """UTC timestamp + host (run_id; git sha added on the measured run)."""
    stamp = rfc3339_micros().replace(":", "").replace(".", "").replace("-", "")[:15]
    return f"{stamp}-{platform.node()}"


@app.command(name="doctor")
def doctor_cmd(
    require_docker: bool = typer.Option(False, "--require-docker", help="Fail if docker is absent (Linux runs)."),
    out: str = typer.Option("", help="Optional path to write doctor.json."),
) -> None:
    """Preflight checks (per-OS; Windows runs a subset)."""
    report = doctor.run_doctor(require_docker=require_docker)
    table = Table(title="ledgerbench doctor")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for c in report.checks:
        status = "[yellow]skip[/]" if c.skipped else ("[green]ok[/]" if c.ok else "[red]FAIL[/]")
        table.add_row(c.name, status, c.detail)
    console.print(table)
    if out:
        from pathlib import Path

        report.save(Path(out))
    raise typer.Exit(code=0 if report.all_ok else 1)


@app.command(name="smoke")
def smoke_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command plan, run nothing."),
    seed: int = typer.Option(-1, help="Override the target-generator seed (default: workload.yaml)."),
) -> None:
    """Windows smoke flow (build/seed-smoke/up/probe/teardown) -- functional only."""
    report = smoke.run_smoke(dry_run=dry_run, seed=None if seed < 0 else seed)
    table = Table(title=f"ledgerbench smoke [{report.label}] (resources: {report.resource_accounting})")
    table.add_column("step")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for s in report.steps:
        status = "[blue]deferred[/]" if s.deferred else ("[green]ok[/]" if s.ok else "[red]FAIL[/]")
        table.add_row(s.name, status, s.detail)
    console.print(table)
    raise typer.Exit(code=0 if report.ok else 1)


# --------------------------------------------------------------------------- #
# run-all (live, resumable)
# --------------------------------------------------------------------------- #
def _resolve_resume_run_id(run_id: str | None, resume: bool) -> tuple[str, bool]:
    """Resolve (run_id, is_resume) from the flags.

    no flags                  -> NEW run (fresh run_id)
    --resume (no --run-id)    -> most recent unfinished ledger (error if none)
    --resume --run-id X       -> resume exactly X (config_hash guard in ledger)
    --run-id X (no --resume)  -> resume X if it exists, else a NEW run named X
    """
    if resume and run_id is None:
        led = Ledger.latest_unfinished()
        if led is None:
            raise typer.BadParameter(
                "no unfinished run to resume (no results/*/ledger.json with pending "
                "work). Start a fresh run with `run-all` (no --resume)."
            )
        return led.run_id, True
    if run_id is not None:
        existing = Ledger.load(run_id)
        return run_id, existing is not None
    return _new_run_id(), False


def _resume_banner(led: Ledger) -> None:
    done = total = 0
    for c in led.cells:
        for st in ("ladder", "fit", "confirm", "soak"):
            d, t = led.stage_counts(c.cell_id, st)
            done += d
            total += t
    console.print(
        f"[cyan]resuming[/] {led.run_id}: done {done}/{total} probes, "
        f"next: [bold]{led.next_pending()}[/]"
    )


@app.command(name="run-all", epilog=ENV_HELP)
def run_all(
    scale: str = typer.Option("full", help="full|smoke"),
    run_id: str = typer.Option(None, "--run-id", help="Resume/target a specific run id."),
    resume: bool = typer.Option(False, "--resume", help="Resume an existing ledger."),
) -> None:
    """Full phase pipeline, live + ledger-backed + resumable.

    ABORTS at the capacity gate unless it passes: a run whose DB headroom was
    never established is not a language comparison at a fixed latency promise.
    """
    resolved_id, is_resume = _resolve_resume_run_id(run_id, resume)
    ctx = phases.make_context(run_id=resolved_id, scale=scale)
    if is_resume:
        _resume_banner(ctx.led)
    else:
        console.print(f"[green]new run[/] {resolved_id} (scale={scale})")
    try:
        phases.run_all(ctx)
    except phases.PhaseError as exc:
        console.print(f"[red]run-all failed[/]: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]run-all complete[/] {resolved_id} "
        f"(complete={ctx.led.is_complete()})"
    )


# --------------------------------------------------------------------------- #
# per-phase re-run entry points (each requires --run-id)
# --------------------------------------------------------------------------- #
def _cell_or_fail(ctx, cell_id: str) -> phases.Cell:
    for c in phases.CELLS:
        if c.cell_id == cell_id:
            return c
    raise typer.BadParameter(f"unknown cell {cell_id!r}; known: {[c.cell_id for c in phases.CELLS]}")


def _guarded(label: str, fn, *args) -> None:
    """Run a phase function, turning a PhaseError into a clean exit-1 message.

    These commands re-assert the capacity gate before their first probe, so a
    non-passing (or never-run) gate surfaces here as an operator-readable
    refusal instead of a traceback.
    """
    try:
        fn(*args)
    except phases.PhaseError as exc:
        console.print(f"[red]{label} refused[/]: {exc}")
        raise typer.Exit(code=1) from exc


@app.command(epilog=ENV_HELP)
def gate(
    run_id: str = typer.Option(..., "--run-id", help="Run id to (re)run the gate in."),
    scale: str = typer.Option("smoke", help="full|smoke"),
) -> None:
    """DB capacity gate (create-only ladder + levers), wired to phases.

    A non-passing verdict FAILS this command (exit 1) and blocks every measured
    stage in the run until the gate passes or the opt-out is set.
    """
    ctx = phases.make_context(run_id=run_id, scale=scale)
    _guarded("gate", phases.run_capacity_gate_phase, ctx)
    console.print(f"[green]gate done[/] {run_id}: {ctx.led.phase('capacity_gate').data}")


@app.command(epilog=ENV_HELP)
def ladder(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option("smoke", help="full|smoke"),
) -> None:
    """Geometric mixed ladder -> bracket the SLO crossing (per-cell).

    Re-asserts the run's capacity gate first: it adopts the gate's EFFECTIVE
    workload (so the levers the gate applied are actually in force) and refuses
    to probe when the recorded verdict is not `pass`, or when no gate ever ran
    in this run.
    """
    ctx = phases.make_context(run_id=run_id, scale=scale)
    _guarded("ladder", phases.run_ladder, ctx, _cell_or_fail(ctx, cell))
    console.print(f"[green]ladder done[/] {cell}: {ctx.led.stage(cell, 'ladder').data}")


@app.command(epilog=ENV_HELP)
def confirm(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option("smoke", help="full|smoke"),
) -> None:
    """Confirm reps at bracket rates (RCB interleave, per-cell).

    Gate-guarded exactly like `ladder` (both the fit and the confirm probes
    re-assert the recorded capacity-gate verdict and effective workload).
    """
    ctx = phases.make_context(run_id=run_id, scale=scale)
    c = _cell_or_fail(ctx, cell)
    # fit must precede confirm (computes bracket rates)
    _guarded("confirm", phases.run_fit, ctx, c)
    _guarded("confirm", phases.run_confirm, ctx, c)
    console.print(f"[green]confirm done[/] {cell}: {ctx.led.stage(cell, 'confirm').data}")


@app.command(epilog=ENV_HELP)
def soak(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option("smoke", help="full|smoke"),
) -> None:
    """Soak + stationarity (per-cell).

    Gate-guarded exactly like `ladder`.
    """
    ctx = phases.make_context(run_id=run_id, scale=scale)
    _guarded("soak", phases.run_soak, ctx, _cell_or_fail(ctx, cell))
    console.print(f"[green]soak done[/] {cell}: {ctx.led.stage(cell, 'soak').data}")


@app.command()
def analyze(run_id: str = typer.Argument(..., help="results/<run_id> to analyze")) -> None:
    """Knee fits (pooled + per language), stationarity, per-rate paired verdicts,
    the CPU-vs-rate curve and the resource roll-up -> report/.

    The console table below is a summary of `report/report.md`; every caveat the
    numbers carry (what the printed knee interval is, what the pooled knee
    actually pooled, which language met the latency promise) lives there.
    """
    from . import analyze as analyze_mod

    try:
        result = analyze_mod.analyze_run(run_id)
    except FileNotFoundError as exc:
        console.print(f"[red]analyze failed[/]: {exc}")
        raise typer.Exit(code=1) from exc

    if not result.analyzable:
        console.print(
            f"[red]no analyzable cells[/] in {run_id}: no cell has a completed "
            f"ladder stage to fit a knee from. Run the cells phase first "
            f"(`run-all` / `ladder`)."
        )
        raise typer.Exit(code=1)

    # Column names must not promise more than the numbers are. The interval is a
    # bracketed refit spread, NOT a 95% CI (report.md explains why it cannot be
    # one at ladder rep counts), and the paired verdict is per offered rate; the
    # old single-verdict column silently showed the lowest rate only.
    table = Table(title=f"knee fits: {run_id} (SLO p99 = {result.slo_ms:g} ms)")
    table.add_column("cell")
    table.add_column("knee rps (pooled)")
    table.add_column("bootstrap interval")
    table.add_column("inferential?")
    table.add_column("paired verdict (per offered rate)", overflow="fold")
    for c in result.cells:
        k = c.knee
        if not k.crossed:
            knee_s, ci_s, valid_s = "[dim]no crossing[/]", "n/a", "n/a"
        elif k.knee_rps is None:
            knee_s, ci_s, valid_s = "[dim]unfit[/]", "n/a", "n/a"
        else:
            knee_s = f"{k.knee_rps:.0f}"
            ci_s = f"[{k.ci_low_rps:.0f}, {k.ci_high_rps:.0f}]"
            valid_s = "[green]yes[/]" if k.ci_valid else "[yellow]NOT a CI[/]"
        verdict_s = (
            "; ".join(f"{p.verdict} @ {p.rate} rps" for p in c.paired_by_rate)
            or "[dim]n/a[/]"
        )
        table.add_row(c.cell_id, knee_s, ci_s, valid_s, verdict_s)
    console.print(table)
    console.print(f"[green]wrote[/] {result.fits_path}")
    console.print(f"[green]wrote[/] {result.report_path}")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _state_markup(state: State | str) -> str:
    s = state.value if isinstance(state, State) else str(state)
    return {
        "done": "[green]done[/]",
        "running": "[yellow]running[/]",
        "pending": "[dim]pending[/]",
        "failed": "[red]FAILED[/]",
    }.get(s, s)


@app.command()
def status(run_id: str = typer.Argument(None, help="run id (default: latest ledger)")) -> None:
    """Rich phase/cell/stage table + a machine-readable STATUS summary line."""
    led = Ledger.load(run_id) if run_id else Ledger.latest()
    if led is None:
        console.print("[red]no ledger found[/] (no results/*/ledger.json yet)")
        raise typer.Exit(code=1)

    phase_table = Table(title=f"phases: {led.run_id}")
    phase_table.add_column("phase")
    phase_table.add_column("state")
    phase_table.add_column("detail", overflow="fold")
    for name in PHASE_ORDER:
        if name == "cells":
            continue
        rec = led.phases.get(name)
        st = rec.state if rec else State.PENDING
        detail = ""
        if rec and rec.data:
            keys = [k for k in ("verdict", "scale", "skipped", "tier", "equivalence") if k in rec.data]
            detail = ", ".join(f"{k}={rec.data[k]}" for k in keys)
        phase_table.add_row(name, _state_markup(st), detail)
    console.print(phase_table)

    if led.cells:
        cell_table = Table(title="cells")
        cell_table.add_column("cell")
        for st in ("ladder", "fit", "confirm", "soak"):
            cell_table.add_column(st)
        for c in led.cells:
            row = [c.cell_id]
            for st in ("ladder", "fit", "confirm", "soak"):
                stage = getattr(c, st)
                d, t = led.stage_counts(c.cell_id, st)
                label = _state_markup(stage.state)
                if t:
                    label += f" {d}/{t}"
                row.append(label)
            cell_table.add_row(*row)
        console.print(cell_table)

    nxt = led.next_pending()
    if led.is_complete():
        run_state = "complete"
    elif any(r.state == State.FAILED for r in led.phases.values()) or any(
        getattr(c, st).state == State.FAILED
        for c in led.cells for st in ("ladder", "fit", "confirm", "soak")
    ):
        run_state = "failed"
    else:
        run_state = "running"
    console.print(f"[bold]next:[/] {nxt}")
    console.print(f"STATUS run_id={led.run_id} state={run_state} next={nxt}")
    raise typer.Exit(code=0)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        console.print("[red]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
