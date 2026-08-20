"""cgroup v2 sampler. Linux: 50 ms anon + cpu.stat deltas.

Path resolution is NEVER hardcoded: given a container name,
`docker inspect` -> PID -> /proc/<pid>/cgroup unified `0::<path>` line ->
/sys/fs/cgroup/<path>/. Resolved once per probe (fresh container => fresh path).

Sampling: a dedicated thread (not asyncio) reads memory.stat `anon` + cpu.stat
`usage_usec`/`nr_throttled`/`throttled_usec` at 50 ms into per-container CSVs.
The thread self-pins to the housekeeping core (the runner self-pins).
Window-end reads: memory.peak, memory.stat.file; memory.events for OOM.

Windows / non-Linux: the same interface, but the sampler is a NO-OP that records
`"resource_accounting": "disabled (non-Linux)"` so smoke runs functionally
without it. The interface is identical so the runner has one code path.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .compose import container_pid
from .util import paths

IS_LINUX = sys.platform.startswith("linux")
CGROUP_ROOT = Path("/sys/fs/cgroup")
SAMPLE_INTERVAL_S = 0.050  # 50 ms


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #
def resolve_cgroup_path(container_name: str, *, pid: int | None = None) -> Path | None:
    """Resolve the unified cgroup dir for a container. Linux only.

    Returns None on non-Linux or if resolution fails (caller treats as no-op).
    """
    if not IS_LINUX:
        return None
    if pid is None:
        pid = container_pid(container_name)
    if not pid:
        return None
    cgline = Path(f"/proc/{pid}/cgroup")
    if not cgline.exists():
        return None
    for line in cgline.read_text().splitlines():
        # unified v2 line: "0::<path>"
        if line.startswith("0::"):
            rel = line.split("::", 1)[1].lstrip("/")
            return CGROUP_ROOT / rel
    return None


def _read_int_field(stat_path: Path, key: str) -> int | None:
    try:
        for line in stat_path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == key:
                return int(parts[1])
    except OSError:
        return None
    return None


def _read_single_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# sample record
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    monotonic_s: float
    cpu_usage_usec: int | None
    nr_throttled: int | None
    throttled_usec: int | None
    mem_anon: int | None


@dataclass
class WindowResult:
    container: str
    enabled: bool
    samples: list[Sample] = field(default_factory=list)
    cpu_usage_delta_usec: int | None = None
    nr_throttled_end: int | None = None
    throttled_usec_end: int | None = None
    mem_anon_p99: int | None = None
    mem_peak: int | None = None
    mem_file_end: int | None = None
    oom_events: int | None = None
    note: str = ""

    def csv_lines(self) -> str:
        head = "monotonic_s,cpu_usage_usec,nr_throttled,throttled_usec,mem_anon"
        rows = [
            f"{s.monotonic_s:.6f},"
            f"{'' if s.cpu_usage_usec is None else s.cpu_usage_usec},"
            f"{'' if s.nr_throttled is None else s.nr_throttled},"
            f"{'' if s.throttled_usec is None else s.throttled_usec},"
            f"{'' if s.mem_anon is None else s.mem_anon}"
            for s in self.samples
        ]
        return "\n".join([head, *rows]) + "\n"

    def write_csv(self, path: Path) -> None:
        paths.write_text_atomic(path, self.csv_lines())


# --------------------------------------------------------------------------- #
# sampler
# --------------------------------------------------------------------------- #
class CgroupSampler:
    """50 ms cgroup v2 sampler for one container. No-op on non-Linux."""

    def __init__(self, container_name: str, *, pid: int | None = None, pin_core: int | None = None):
        self.container = container_name
        self.pin_core = pin_core
        self.enabled = IS_LINUX
        self._cg = resolve_cgroup_path(container_name, pid=pid) if IS_LINUX else None
        if self._cg is None:
            self.enabled = False
        self._samples: list[Sample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu_start: int | None = None

    @property
    def note(self) -> str:
        return "" if self.enabled else "disabled (non-Linux)"

    def _pin_self(self) -> None:
        if self.pin_core is not None and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, {self.pin_core})
            except OSError:
                pass

    def _sample(self) -> Sample:
        cg = self._cg
        assert cg is not None
        cpu = _read_int_field(cg / "cpu.stat", "usage_usec")
        return Sample(
            monotonic_s=time.monotonic(),
            cpu_usage_usec=cpu,
            nr_throttled=_read_int_field(cg / "cpu.stat", "nr_throttled"),
            throttled_usec=_read_int_field(cg / "cpu.stat", "throttled_usec"),
            mem_anon=_read_int_field(cg / "memory.stat", "anon"),
        )

    def _loop(self) -> None:
        self._pin_self()
        next_t = time.monotonic()
        while not self._stop.is_set():
            self._samples.append(self._sample())
            next_t += SAMPLE_INTERVAL_S
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_t = time.monotonic()  # fell behind; resync

    def start(self) -> None:
        if not self.enabled:
            return
        assert self._cg is not None
        self._cpu_start = _read_int_field(self._cg / "cpu.stat", "usage_usec")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"cgroup-{self.container}", daemon=True)
        self._thread.start()

    def stop(self) -> WindowResult:
        if not self.enabled:
            return WindowResult(container=self.container, enabled=False, note=self.note)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        cg = self._cg
        assert cg is not None
        cpu_end = _read_int_field(cg / "cpu.stat", "usage_usec")
        delta = None
        if self._cpu_start is not None and cpu_end is not None:
            delta = cpu_end - self._cpu_start
        anon_vals = sorted(s.mem_anon for s in self._samples if s.mem_anon is not None)
        return WindowResult(
            container=self.container,
            enabled=True,
            samples=list(self._samples),
            cpu_usage_delta_usec=delta,
            nr_throttled_end=_read_int_field(cg / "cpu.stat", "nr_throttled"),
            throttled_usec_end=_read_int_field(cg / "cpu.stat", "throttled_usec"),
            mem_anon_p99=_percentile(anon_vals, 0.99),
            mem_peak=_read_single_int(cg / "memory.peak"),
            mem_file_end=_read_int_field(cg / "memory.stat", "file"),
            oom_events=_read_int_field(cg / "memory.events", "oom_kill"),
        )


def _percentile(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[idx]


def disabled_result(container_name: str) -> WindowResult:
    """Explicit no-op record for the manifest on non-Linux (smoke)."""
    return WindowResult(container=container_name, enabled=False, note="disabled (non-Linux)")
