"""Resumable run-ledger: a single pydantic-validated JSON file.

`results/<run_id>/ledger.json`. Append-safe via write-temp-rename (util.paths).
Not a DB: human-readable, git-diffable, probe-granular resume. On start the
runner loads the ledger and skips any phase/cell/probe whose state == "done";
re-runs running/pending. A probe is `done` only after its artifacts are flushed
and checksummed (the runner enforces that ordering; this module is the schema +
the resume helpers).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .util import paths
from .util.clock import rfc3339_micros

LEDGER_SCHEMA_VERSION = 1


class State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PhaseRecord(BaseModel):
    state: State = State.PENDING
    ts: str | None = None
    # Free-form per-phase payload (e.g. dump_sha256, levers_fired, verdict).
    data: dict = Field(default_factory=dict)


class ProbeRecord(BaseModel):
    block: int
    slot_order: list[str] = Field(default_factory=list)  # e.g. ["dotnet","go"]
    lang: Literal["go", "dotnet"]
    rate: int
    state: State = State.PENDING
    artifact: str | None = None         # relative path to the .bin
    achieved: float | None = None
    error_rate: float | None = None
    ts: str | None = None


class StageRecord(BaseModel):
    """A ladder/fit/confirm/soak stage within a cell."""

    state: State = State.PENDING
    data: dict = Field(default_factory=dict)
    probes: list[ProbeRecord] = Field(default_factory=list)


class CellRecord(BaseModel):
    cell_id: str
    ladder: StageRecord = Field(default_factory=StageRecord)
    fit: StageRecord = Field(default_factory=StageRecord)
    confirm: StageRecord = Field(default_factory=StageRecord)
    soak: StageRecord = Field(default_factory=StageRecord)


class Ledger(BaseModel):
    run_id: str
    schema_version: int = LEDGER_SCHEMA_VERSION
    created_utc: str = Field(default_factory=rfc3339_micros)
    config_hash: str
    rng_seed: int                       # interleave / within-block order seed
    # The scale profile the run was started with (full|smoke). Pinned like
    # config_hash: resuming with a different --scale would silently mix
    # measurement windows (120 s vs 10 s) inside the same rungs. None only on
    # pre-field ledgers (backfilled on first load_or_create).
    scale: str | None = None
    # The targets-file request count the run STARTED with (LEDGERBENCH_TARGET_COUNT
    # or the profile default). Pinned like `scale`: ensure_targets regenerates the
    # file whenever its meta (including count) differs from the effective profile,
    # so an env change on resume would silently swap the working set mid-run. The
    # pinned value wins on resume. None only on pre-field ledgers (backfilled).
    target_count: int | None = None
    phases: dict[str, PhaseRecord] = Field(default_factory=dict)
    cells: list[CellRecord] = Field(default_factory=list)

    # ----- persistence ----------------------------------------------------- #
    def path(self) -> Path:
        return paths.run_dir(self.run_id) / "ledger.json"

    def save(self) -> None:
        """Atomically persist (write-temp-rename) so a crash never truncates."""
        paths.write_text_atomic(self.path(), self.model_dump_json(indent=2))

    @classmethod
    def load(cls, run_id: str) -> "Ledger | None":
        p = paths.run_dir(run_id) / "ledger.json"
        if not p.exists():
            return None
        return cls.model_validate_json(p.read_text(encoding="utf-8"))

    @classmethod
    def latest(cls) -> "Ledger | None":
        """Most recently created ledger across results/*/ledger.json (or None)."""
        rdir = paths.results_dir()
        if not rdir.is_dir():
            return None
        candidates: list[tuple[str, Ledger]] = []
        for led_path in rdir.glob("*/ledger.json"):
            try:
                led = cls.model_validate_json(led_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - ignore unparseable dirs
                continue
            candidates.append((led.created_utc, led))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        return candidates[-1][1]

    @classmethod
    def latest_unfinished(cls) -> "Ledger | None":
        """Most recent ledger that is NOT fully complete (resume target)."""
        rdir = paths.results_dir()
        if not rdir.is_dir():
            return None
        candidates: list[tuple[str, Ledger]] = []
        for led_path in rdir.glob("*/ledger.json"):
            try:
                led = cls.model_validate_json(led_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not led.is_complete():
                candidates.append((led.created_utc, led))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        return candidates[-1][1]

    @classmethod
    def load_or_create(
        cls, run_id: str, *, config_hash: str, rng_seed: int, scale: str | None = None,
        target_count: int | None = None,
    ) -> "Ledger":
        existing = cls.load(run_id)
        if existing is not None:
            if existing.config_hash != config_hash:
                raise RuntimeError(
                    f"config_hash changed for run {run_id}: spec edited since the "
                    f"run started ({existing.config_hash} != {config_hash}). "
                    f"Start a fresh run_id."
                )
            if scale is not None:
                if existing.scale is None:
                    existing.scale = scale  # backfill a pre-field ledger
                    existing.save()
                elif existing.scale != scale:
                    raise RuntimeError(
                        f"scale mismatch for run {run_id}: started as "
                        f"{existing.scale!r}, resume requested {scale!r}. Mixing "
                        f"scales would blend measurement windows inside the same "
                        f"rungs. Resume with --scale {existing.scale}."
                    )
            if target_count is not None and existing.target_count is None:
                existing.target_count = target_count  # backfill a pre-field ledger
                existing.save()
            return existing
        led = cls(run_id=run_id, config_hash=config_hash, rng_seed=rng_seed,
                  scale=scale, target_count=target_count)
        led.save()
        return led

    # ----- phase helpers --------------------------------------------------- #
    def phase(self, name: str) -> PhaseRecord:
        rec = self.phases.get(name)
        if rec is None:
            rec = PhaseRecord()
            self.phases[name] = rec
        return rec

    def phase_done(self, name: str) -> bool:
        rec = self.phases.get(name)
        return rec is not None and rec.state == State.DONE

    def mark_phase(self, name: str, state: State, **data: object) -> None:
        rec = self.phase(name)
        rec.state = state
        rec.ts = rfc3339_micros()
        if data:
            rec.data.update(data)
        self.save()

    # ----- cell helpers ---------------------------------------------------- #
    def cell(self, cell_id: str) -> CellRecord:
        for c in self.cells:
            if c.cell_id == cell_id:
                return c
        c = CellRecord(cell_id=cell_id)
        self.cells.append(c)
        return c

    def stage(self, cell_id: str, stage: str) -> StageRecord:
        cell = self.cell(cell_id)
        rec: StageRecord = getattr(cell, stage)
        return rec

    def mark_stage(self, cell_id: str, stage: str, state: State, **data: object) -> None:
        rec = self.stage(cell_id, stage)
        rec.state = state
        if data:
            rec.data.update(data)
        self.save()

    # ----- resume logic ---------------------------------------------------- #
    def pending_probes(self, cell_id: str, stage: str) -> list[ProbeRecord]:
        """Probes not yet `done` (resume re-runs running/pending/failed)."""
        return [p for p in self.stage(cell_id, stage).probes if p.state != State.DONE]

    def reset_orphan_blocks(self, cell_id: str, stage: str) -> list[int]:
        """RCB block-integrity rule on resume.

        A block is one interleave slot holding exactly one {go, dotnet} pair at a
        single rate. If a crash leaves a block with EXACTLY ONE member done (its
        sibling pending/running/failed), the paired comparison for that slot is
        compromised: the surviving member ran under different ambient conditions
        than its re-run sibling will. So the whole block is reset: the done member
        is flipped back to pending and the stage is flagged
        `redone_after_resume=true`.

        Returns the list of block ids that were reset. Persists if anything
        changed. Idempotent: a block that is fully done or fully pending is left
        alone. Blocks are keyed by (block, rate) so multi-rate confirm stages
        (two bracket rates) don't collide.
        """
        rec = self.stage(cell_id, stage)
        by_block: dict[tuple[int, int], list[ProbeRecord]] = {}
        for p in rec.probes:
            by_block.setdefault((p.block, p.rate), []).append(p)

        reset_blocks: list[int] = []
        for (block, _rate), members in by_block.items():
            done = [p for p in members if p.state == State.DONE]
            if 0 < len(done) < len(members):
                for p in done:
                    p.state = State.PENDING
                    p.artifact = None
                    p.achieved = None
                    p.error_rate = None
                    p.ts = None
                reset_blocks.append(block)

        if reset_blocks:
            rec.data["redone_after_resume"] = True
            rec.data["redone_blocks"] = sorted(set(reset_blocks))
            self.save()
        return sorted(set(reset_blocks))

    def mark_probe_done(
        self,
        cell_id: str,
        stage: str,
        probe: ProbeRecord,
        *,
        artifact: str,
        achieved: float,
        error_rate: float,
    ) -> None:
        """Flip a probe to done after all its artifacts are flushed.

        The caller must have already written the .bin + cgroup CSV + manifest
        fragment and checksummed them. This only records the
        terminal state and persists the ledger.
        """
        probe.state = State.DONE
        probe.artifact = artifact
        probe.achieved = achieved
        probe.error_rate = error_rate
        probe.ts = rfc3339_micros()
        self.save()

    # ----- status / completion -------------------------------------------- #
    def cell_ids(self) -> list[str]:
        return [c.cell_id for c in self.cells]

    def add_probe(self, cell_id: str, stage: str, probe: ProbeRecord) -> None:
        self.stage(cell_id, stage).probes.append(probe)

    def stage_counts(self, cell_id: str, stage: str) -> tuple[int, int]:
        """(done, total) probe counts for a cell stage."""
        rec = self.stage(cell_id, stage)
        done = sum(1 for p in rec.probes if p.state == State.DONE)
        return done, len(rec.probes)

    def is_complete(self) -> bool:
        """True iff every recorded phase is done AND every cell stage is done.

        A run with no cells yet recorded is NOT complete (cells phase pending).
        """
        if not self.phases:
            return False
        if any(rec.state != State.DONE for rec in self.phases.values()):
            return False
        # host_restore is the terminal phase; require it present + done.
        if not self.phase_done("host_restore"):
            return False
        for c in self.cells:
            for st in ("ladder", "fit", "confirm", "soak"):
                if getattr(c, st).state != State.DONE:
                    return False
        return True

    def next_pending(self) -> str:
        """Human/machine-readable label of the next not-done unit (phase/cell/stage)."""
        for name in PHASE_ORDER:
            rec = self.phases.get(name)
            if name == "cells":
                # the synthetic "cells" marker: descend into cell stages
                for c in self.cells:
                    for st in ("ladder", "fit", "confirm", "soak"):
                        if getattr(c, st).state != State.DONE:
                            return f"{c.cell_id}/{st}"
                if rec is None or rec.state != State.DONE:
                    return "cells"
                continue
            if rec is None or rec.state != State.DONE:
                return name
        return "complete"


# Canonical phase order for status/next-pending. "cells" is a
# synthetic marker the status walker expands into per-cell stages.
PHASE_ORDER: list[str] = [
    "doctor",
    "host_setup",
    "seed",
    "capacity_gate",
    "validation",
    "cells",
    "variants",
    "host_restore",
]
