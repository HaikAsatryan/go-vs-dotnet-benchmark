"""GET /runtime-stats poller (1 Hz) -> snapshots (spec/runtime-stats.md).

Drives the warmup gates (GC-steady, pools-warm, per-endpoint call counts) and
the per-run GC/pool table + effective-config echo. The recorded RESOURCE numbers
always come from cgroup v2 (cgroups.py), never from this endpoint; runtime-stats
is corroboration + warmup signal only (spec/runtime-stats.md).

Models mirror the frozen shape exactly so the warmup controller and manifest
have one code path across both languages (fields a runtime lacks are null).
"""

from __future__ import annotations

import threading

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .util.clock import monotonic, rfc3339_micros

# Frozen route-template keys for endpoint_calls (spec/runtime-stats.md).
ENDPOINT_KEYS = [
    "GET /invoices/{id}",
    "GET /customers/{id}/invoices",
    "POST /pricing/quote",
    "POST /invoices",
    "POST /invoices/{id}/pdf-stub",
    "GET /healthz",
]


class Build(BaseModel):
    model_config = ConfigDict(extra="allow")
    language: str | None = None
    runtime_version: str | None = None
    service_version: str | None = None
    data_layer: str | None = None
    processor_count: int | None = None


class GcCollections(BaseModel):
    model_config = ConfigDict(extra="allow")
    gen0: int = 0
    gen1: int = 0
    gen2: int = 0

    def total(self) -> int:
        return self.gen0 + self.gen1 + self.gen2


class Gc(BaseModel):
    model_config = ConfigDict(extra="allow")
    collections: GcCollections = Field(default_factory=GcCollections)
    pause_total_seconds: float = 0.0
    heap_in_use_bytes: int = 0
    heap_committed_bytes: int = 0
    heap_goal_bytes: int | None = 0
    allocated_total_bytes: int = 0


class DbPool(BaseModel):
    model_config = ConfigDict(extra="allow")
    max: int = 0
    total: int = 0
    idle: int = 0
    in_use: int = 0
    # The .NET SUT reports null for these acquire counters (Npgsql does not expose
    # the same meters as pgxpool); accept None so the rest of the snapshot
    # (total/max/gc), which the warmup gates actually read, still parses.
    acquire_count: int | None = 0
    empty_acquire_count: int | None = 0
    acquire_wait_total_seconds: float | None = 0.0


class RedisPool(BaseModel):
    model_config = ConfigDict(extra="allow")
    max: int = 0
    total: int = 0
    idle: int = 0
    in_use: int = 0


class RuntimeStats(BaseModel):
    """One /runtime-stats sample (frozen shape, both languages)."""

    model_config = ConfigDict(extra="allow")
    ts: str | None = None
    uptime_seconds: float = 0.0
    build: Build = Field(default_factory=Build)
    config: dict = Field(default_factory=dict)
    gc: Gc = Field(default_factory=Gc)
    db_pool: DbPool = Field(default_factory=DbPool)
    redis_pool: RedisPool = Field(default_factory=RedisPool)
    goroutines_or_threadpool: int = 0
    endpoint_calls: dict[str, int] = Field(default_factory=dict)

    def endpoint_total(self, key: str) -> int:
        return int(self.endpoint_calls.get(key, 0))


class Snapshot(BaseModel):
    """A timestamped sample as captured by the poller (wall + monotonic)."""

    captured_utc: str
    monotonic_s: float
    stats: RuntimeStats


def fetch_once(base_url: str, *, client: httpx.Client | None = None) -> RuntimeStats:
    own = client is None
    cl = client or httpx.Client(timeout=5.0)
    try:
        resp = cl.get(f"{base_url}/runtime-stats")
        resp.raise_for_status()
        return RuntimeStats.model_validate(resp.json())
    finally:
        if own:
            cl.close()


class StatsPoller:
    """Background 1 Hz poller. Thread-based (no asyncio); KISS.

    Collects snapshots into an in-memory list the warmup controller inspects.
    The snapshots drive the warmup gates live and are then discarded; the
    per-window flush to disk was descoped at scope freeze (only the gate verdicts
    and the warmed pool size survive into the ledger).
    """

    def __init__(self, base_url: str, *, hz: float = 1.0):
        self.base_url = base_url
        self.interval = 1.0 / hz if hz > 0 else 1.0
        self.snapshots: list[Snapshot] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _loop(self) -> None:
        with httpx.Client(timeout=5.0) as client:
            next_t = monotonic()
            while not self._stop.is_set():
                try:
                    stats = fetch_once(self.base_url, client=client)
                    snap = Snapshot(
                        captured_utc=rfc3339_micros(),
                        monotonic_s=monotonic(),
                        stats=stats,
                    )
                    with self._lock:
                        self.snapshots.append(snap)
                except (httpx.HTTPError, ValueError):
                    # transient HTTP, or a /runtime-stats payload with null fields
                    # the frozen model rejects: skip this tick (warmup degrades to
                    # under-warmed rather than killing the poller thread).
                    pass
                next_t += self.interval
                sleep_for = next_t - monotonic()
                if sleep_for > 0:
                    self._stop.wait(sleep_for)
                else:
                    next_t = monotonic()  # fell behind; resync

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("poller already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runtime-stats-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self.snapshots[-1] if self.snapshots else None

    def all(self) -> list[Snapshot]:
        with self._lock:
            return list(self.snapshots)
