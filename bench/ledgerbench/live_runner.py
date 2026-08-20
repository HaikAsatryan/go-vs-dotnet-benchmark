"""Live ProbeRunner: fresh SUT per probe, warmup, attack, pull+checksum artifacts.

Implements the `phases.ProbeRunner` protocol against real docker compose + the
vegeta container, reusing the patterns in smoke.py (compose up/exec, vegeta
attack, /runtime-stats + cgroup deltas).

Probe lifecycle:
  - FRESH SUT container per probe (compose stop+up), the vegeta container
    PERSISTS across probes.
  - `ensure_targets` is idempotent: targets are generated once per (lang, scale)
    per run and re-shipped into the vegeta container if the bench-io volume lost
    them (e.g. after a reboot). It also pins the MIX the file was generated from
    (targets.meta.json): if the capacity gate's write_share lever changed the
    effective workload, a stale targets file is regenerated instead of silently
    replaying the pre-lever mix.
  - warmup BEFORE every probe via warmup.py gates (poll /runtime-stats at 1 Hz,
    cap from the scale profile); under-warmed -> flagged in the outcome.
  - probe atomicity: write all probe artifacts (vegeta JSON summary + per-second
    p99 CSV + cgroup CSVs), sha256 each into CHECKSUMS, THEN the caller marks the
    probe done. The vegeta .bin stays on the bench-io volume.
  - cgroup sampling on Linux (cgroups.py); on Windows it is a no-op recording
    "disabled (non-Linux)".

On Windows (smoke) the cgroup CSVs are the disabled-marker record; everything
else runs functionally so a full smoke run-all exercises the whole path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import capacity_gate as capgate
from . import cgroups, compose, sut, vegeta
from .compose import ComposeEnv
from .config import SloConfig, WarmupCfg, Workload
from .phases import Cell, ProbeOutcome, ScaleProfile
from .runtimestats import StatsPoller, fetch_once
from .util import paths
from .util.proc import run
from .warmup import WarmupResult, WarmupState
from .vegeta import AttackParams

IS_LINUX = sys.platform.startswith("linux")

_HOST_URL_BY_LANG = {"go": "http://localhost:8081", "dotnet": "http://localhost:8082"}
_NET_URL_BY_LANG = {"go": "http://sut-go:8080", "dotnet": "http://sut-dotnet:8080"}
_PROFILE_BY_LANG = {"go": compose.PROFILE_GO, "dotnet": compose.PROFILE_DOTNET}
_SVC_BY_LANG = {"go": compose.SVC_SUT_GO, "dotnet": compose.SVC_SUT_DOTNET}
_VEGETA_CONTAINER = "ledgerline-vegeta-1"


def _housekeeping_core() -> int:
    """First core of HOUSEKEEPING_CPUS (host-setup default 15): the cgroup
    sampler thread pins itself there so its 50 ms reads never run on SUT cores."""
    raw = os.environ.get("HOUSEKEEPING_CPUS", "15")
    head = raw.split(",")[0].split("-")[0].strip()
    try:
        return int(head)
    except ValueError:
        return 15


def _read_stat_field(path: Path, key: str) -> int | None:
    """`<key> <int>` lookup in a cgroup flat-keyed file (memory.stat)."""
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == key:
                return int(parts[1])
    except (OSError, ValueError):
        return None
    return None


def _fault_counters(container: str) -> dict[str, int | None]:
    """memory.stat pgmajfault/pgfault for a container (None off Linux)."""
    cg = cgroups.resolve_cgroup_path(container)
    if cg is None:
        return {"major": None, "minor": None}
    stat = cg / "memory.stat"
    return {"major": _read_stat_field(stat, "pgmajfault"),
            "minor": _read_stat_field(stat, "pgfault")}


def _fault_delta(before: dict, after: dict) -> dict[str, int | None]:
    """Window fault deltas; null (never 0) when either end is unavailable."""
    def _d(key: str) -> int | None:
        b, a = before.get(key), after.get(key)
        return None if b is None or a is None else a - b
    return {"major_faults_delta": _d("major"), "minor_faults_delta": _d("minor")}


def _cache_delta(before: dict, after: dict) -> dict:
    """Redis keyspace hit/miss delta over the window, with explicit source.

    `source: "not_measured"` when either boundary sample failed; a null hit
    rate must never be read as a measured 0.
    """
    hits_b, miss_b = before.get("keyspace_hits"), before.get("keyspace_misses")
    hits_a, miss_a = after.get("keyspace_hits"), after.get("keyspace_misses")
    if None in (hits_b, miss_b, hits_a, miss_a):
        return {"keyspace_hits": None, "keyspace_misses": None, "hit_rate": None,
                "source": "not_measured",
                "note": "redis INFO stats unavailable at a window boundary"}
    hits, misses = hits_a - hits_b, miss_a - miss_b
    total = hits + misses
    return {
        "keyspace_hits": hits, "keyspace_misses": misses,
        "hit_rate": (hits / total) if total > 0 else None,
        "source": "redis INFO stats delta over the measured window",
        "note": "" if total > 0 else "no keyspace operations in the window",
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LiveProbeRunner:
    run_id: str
    profile: ScaleProfile
    workload: Workload
    slo: SloConfig
    bench_overlay: bool = field(default=IS_LINUX)
    # Compose env forced onto EVERY invocation this runner makes: the capacity
    # gate's realized levers (PG/vegeta cpuset move, CACHE_TTL_SECONDS). Empty
    # unless a lever fired; set through `apply_effective`.
    env_overrides: dict[str, str] = field(default_factory=dict)
    # which (lang, cell) targets have been generated+shipped this run
    _shipped: set[tuple[str, str]] = field(default_factory=set)

    # ----- effective workload / topology (capacity-gate levers) ----------- #
    def apply_effective(
        self,
        *,
        workload: Workload | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        """Adopt the EFFECTIVE workload/topology the capacity gate resolved.

        The gate's levers change what the cells run (mix, cache TTL, PG cores).
        Before this existed the levers were computed into a throwaway object and
        the cells kept generating targets from the frozen spec workload, so a
        fired lever changed nothing. Called by the capacity-gate phase on a
        fresh run AND on every resume (the ledger replays the same values).
        """
        if workload is not None:
            self.workload = workload
            # Force targets regeneration: the mix on disk predates the lever.
            self._shipped.clear()
        if env_overrides:
            self.env_overrides.update(env_overrides)

    def _env(self, *, profiles: list[str] | None = None, bench_overlay: bool | None = None) -> ComposeEnv:
        """ComposeEnv with the realized lever env merged in (single choke point)."""
        env = ComposeEnv.base(
            bench_overlay=self.bench_overlay if bench_overlay is None else bench_overlay,
            profiles=list(profiles or []),
        )
        env.extra_env.update(self.env_overrides)
        return env

    def apply_topology(self) -> dict:
        """Recreate the containers whose cpuset the pg_cores lever moved.

        A cpuset is baked into a container at create time, so PG (and the donor,
        vegeta) must be RECREATED for the lever to take effect: an `up -d`
        without --force-recreate would leave PG on its old 3 cores while the
        manifest claimed 4. Returns a small record for the ledger.
        """
        moved = {k: v for k, v in self.env_overrides.items() if k.endswith("_CPUS")}
        if not moved:
            return {"recreated": [], "cpusets": {}}
        env = self._env(profiles=[compose.PROFILE_LOAD])
        res = compose.compose(
            env,
            ["up", "-d", "--wait", "--force-recreate",
             compose.SVC_POSTGRES, compose.SVC_VEGETA],
            check=False, timeout=600,
        )
        return {
            "recreated": [compose.SVC_POSTGRES, compose.SVC_VEGETA],
            "cpusets": moved,
            "ok": res.ok,
            "detail": res.stderr.strip()[-300:] if not res.ok else "",
        }

    # ----- one-time image build (idempotent) ------------------------------ #
    def prepare(self) -> None:
        """Build the SUT + vegeta + seed images once (idempotent; cached).

        Called by run_all before any phase so a fresh checkout works without a
        separate `make build`. docker reuses the layer cache, so a re-run is fast.
        """
        compose.compose(
            self._env(),
            ["build", compose.SVC_SUT_GO, compose.SVC_SUT_DOTNET,
             compose.SVC_VEGETA, compose.SVC_SEED],
            check=False, timeout=3600,
        )

    # ----- clean DB for a fresh seed -------------------------------------- #
    def reset_db(self) -> None:
        """Drop the pgdata volume so a fresh seed applies schema.sql cleanly.

        Only invoked on the fresh-seed branch (resume verify-only never calls
        this). `down -v` removes the named pgdata volume; the next compose `up`
        re-initializes PG from initdb, and the seeder applies the schema fresh.
        """
        env = self._env(
            profiles=[compose.PROFILE_GO, compose.PROFILE_DOTNET,
                      compose.PROFILE_LOAD, compose.PROFILE_SEED],
        )
        compose.compose(env, ["down", "-v"], check=False, timeout=300)

    # ----- targets (idempotent per lang+scale per run) -------------------- #
    def ensure_targets(self, lang: str, cell: Cell) -> None:
        key = (lang, cell.cell_id)
        io_dir = paths.run_dir(self.run_id) / "io" / lang
        targets = io_dir / "targets.txt"
        meta_path = io_dir / "targets.meta.json"
        meta = {
            "mix": dict(self.workload.mix.as_pairs()),
            "count": self.profile.target_count,
            "seed": self.workload.rng.target_generator_seed_default,
            "scale": self.profile.scale,
            "base_url": _NET_URL_BY_LANG[lang],
        }
        # Regenerate if missing (fresh run) OR if the file on disk was generated
        # from a DIFFERENT mix/count/seed than the effective workload we now hold
        # (a capacity-gate write_share lever changes the mix; replaying the old
        # file would silently run the pre-lever workload).
        stale = True
        if targets.exists() and meta_path.exists():
            try:
                stale = json.loads(meta_path.read_text(encoding="utf-8")) != meta
            except (OSError, ValueError):
                stale = True
        if stale:
            vegeta.generate_targets(
                self.workload, out_dir=io_dir,
                count=self.profile.target_count,
                seed=self.workload.rng.target_generator_seed_default,
                scale=self.profile.scale, base_url=_NET_URL_BY_LANG[lang],
            )
            paths.write_text_atomic(meta_path, json.dumps(meta, indent=2))
            self._shipped.discard(key)
        env = self._env(profiles=[compose.PROFILE_LOAD])
        # idempotent: re-mkdir + re-cp every probe is cheap and survives a reboot
        # that cleared the bench-io volume.
        present = compose.exec_in(
            env, compose.SVC_VEGETA,
            ["sh", "-c", f"test -f /io/{lang}/targets.txt && echo yes || echo no"],
            check=False,
        )
        if key in self._shipped and "yes" in present.stdout:
            return
        compose.exec_in(env, compose.SVC_VEGETA, ["sh", "-c", f"mkdir -p /io/{lang}"], check=False)
        compose.compose(
            env, ["cp", str(io_dir) + "/.", f"{compose.SVC_VEGETA}:/io/{lang}/"],
            check=False, timeout=600,
        )
        self._shipped.add(key)

    # ----- one probe ------------------------------------------------------- #
    def run_probe(
        self, *, cell: Cell, stage: str, lang: str, rate: int, block: int,
        duration_s: int, warmup_cap_s: int, artifact_dir: Path,
    ) -> ProbeOutcome:
        profile_obj = _PROFILE_BY_LANG[lang]
        svc = _SVC_BY_LANG[lang]
        host_url = _HOST_URL_BY_LANG[lang]
        env_sut = self._env(profiles=[profile_obj, compose.PROFILE_LOAD])
        env_sut.extra_env.update(cell.extra_env)
        env_load = self._env(profiles=[compose.PROFILE_LOAD])

        # FRESH SUT: stop+recreate so no prior-probe heap leaks across the pair.
        # BOTH SUTs are stopped, not just this probe's: the sibling may be
        # resident from the knob-recording read or a prior phase, and the two
        # share the same cpuset; an idle sibling on the SUT cores during a
        # measured window perturbs the headline.
        env_both = self._env(profiles=[compose.PROFILE_GO, compose.PROFILE_DOTNET])
        compose.compose(
            env_both, ["stop", compose.SVC_SUT_GO, compose.SVC_SUT_DOTNET],
            check=False, timeout=120,
        )
        up = compose.compose(
            env_sut, ["up", "-d", "--wait", "--force-recreate", svc, compose.SVC_VEGETA],
            check=False, timeout=600,
        )
        if not up.ok:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=f"up failed: {up.stderr[-300:]}")
        try:
            sut.wait_ready(host_url, timeout_s=180.0)
        except TimeoutError as exc:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=str(exc))

        # Same cold cache for every probe; warmup repopulates by construction.
        compose.compose(
            self._env(),
            ["exec", "-T", compose.SVC_REDIS, "redis-cli", "FLUSHALL"], check=False,
        )

        self.ensure_targets(lang, cell)

        # cgroup sampler (Linux): resolve fresh path for this fresh container.
        container = f"ledgerline-{svc}-1"
        sampler = cgroups.CgroupSampler(container, pin_core=_housekeeping_core())

        # warmup: short attack while polling /runtime-stats at 1 Hz.
        warm = self._warmup(env_load, lang, host_url, warmup_cap_s)

        # Head-of-window discard at the MEASURED rate (slo
        # ladder.warmup_discard_seconds): runs before the cgroup sampler starts
        # and before the measured .bin, so neither the pooled p99 nor the CPU/
        # memory window includes the JIT-retiering transient at this rate.
        if self.profile.warmup_discard_s > 0:
            d_params = AttackParams(
                rate=rate, duration_s=self.profile.warmup_discard_s,
                max_workers=self.profile.max_workers,
                connections=self.profile.connections, timeout_s=self.profile.timeout_s,
            )
            d_argv = vegeta.build_attack_argv(
                targets_in_container="targets.txt",
                out_bin_in_container=f"/io/{lang}/discard.bin", params=d_params,
            )
            compose.exec_in(
                env_load, compose.SVC_VEGETA,
                ["sh", "-c", f"cd /io/{lang} && {' '.join(d_argv)}"], check=False,
            )

        # measured attack.
        out_bin = f"/io/{lang}/{stage}_r{rate}_b{block}.bin"
        params = AttackParams(
            rate=rate, duration_s=duration_s,
            max_workers=self.profile.max_workers,
            connections=self.profile.connections, timeout_s=self.profile.timeout_s,
        )
        attack = vegeta.build_attack_argv(
            targets_in_container="targets.txt", out_bin_in_container=out_bin, params=params,
        )
        # Window-boundary samples (never DURING the measured window, so they
        # cannot perturb it): Redis keyspace hit/miss counters (spec/cache.md
        # "measured and asserted in the run manifest") and the SUT cgroup's
        # page-fault counters (the warmup major-fault disclosure).
        cache_before = self._redis_keyspace_stats()
        faults_before = _fault_counters(container)
        sampler.start()
        res = compose.exec_in(
            env_load, compose.SVC_VEGETA,
            ["sh", "-c", f"cd /io/{lang} && {' '.join(attack)}"], check=False,
        )
        window = sampler.stop()
        cache_after = self._redis_keyspace_stats()
        faults_after = _fault_counters(container)
        if not res.ok:
            compose.compose(env_sut, ["stop", svc], check=False)
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=f"attack failed: {res.stderr[-300:]}")

        # pull artifacts: JSON summary + per-second p99 CSV + cgroup CSV.
        rep_proc = compose.exec_in(
            env_load, compose.SVC_VEGETA,
            ["vegeta", "report", "-type=json", out_bin], check=False,
        )
        try:
            report = vegeta.parse_report_proc(rep_proc)
        except RuntimeError as exc:
            compose.compose(env_sut, ["stop", svc], check=False)
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail=str(exc)[:300])

        artifact_dir.mkdir(parents=True, exist_ok=True)
        summary_path = artifact_dir / f"{lang}.summary.json"
        paths.write_text_atomic(summary_path, rep_proc.stdout)

        per_sec = vegeta.encode_per_second_p99(
            exec_prefix=[*env_load.base_argv(), "exec", "-T", compose.SVC_VEGETA],
            bin_in_container=out_bin,
        )
        p99_csv = artifact_dir / f"{lang}.p99_per_second.csv"
        vegeta.write_per_second_p99_csv(per_sec, p99_csv)
        per_sec_values = [v for _, v in per_sec]

        cgroup_csv = artifact_dir / f"cpu_mem_{lang}.csv"
        window.write_csv(cgroup_csv)

        # Window-end scalars are the HEADLINE inputs (CPU delta, anon p99) and
        # the methodology gates (memory.peak, OOM, throttle counters): persist
        # them; the raw 50 ms CSV alone loses the end-of-window reads.
        cg_summary_path = artifact_dir / f"cpu_mem_{lang}.summary.json"
        faults = _fault_delta(faults_before, faults_after)
        paths.write_text_atomic(cg_summary_path, json.dumps({
            "container": window.container,
            "enabled": window.enabled,
            "cpu_usage_delta_usec": window.cpu_usage_delta_usec,
            "nr_throttled_end": window.nr_throttled_end,
            "throttled_usec_end": window.throttled_usec_end,
            "mem_anon_p99": window.mem_anon_p99,
            "mem_peak": window.mem_peak,
            "mem_file_end": window.mem_file_end,
            "oom_events": window.oom_events,
            # memory.stat page-fault deltas over the measured window; null (not
            # 0) when the counters were unreadable (non-Linux / no cgroup).
            "major_faults_delta": faults["major_faults_delta"],
            "minor_faults_delta": faults["minor_faults_delta"],
            "note": window.note,
        }, indent=2))

        # Redis keyspace hit/miss delta for THIS window (spec/cache.md hit-rate
        # assertion input). Written even when unavailable, with source recorded,
        # so the manifest can say "not measured" instead of implying zero.
        cache_path = artifact_dir / f"cache_stats_{lang}.json"
        paths.write_text_atomic(cache_path, json.dumps(
            _cache_delta(cache_before, cache_after), indent=2))

        # METHODOLOGY: OOM = reported failure (never a silently-degraded probe).
        if window.enabled and (window.oom_events or 0) > 0:
            compose.compose(env_sut, ["stop", svc], check=False)
            return ProbeOutcome(
                achieved=0, error_rate=1.0, p99_ms=0, artifact="", ok=False,
                detail=f"OOM kill(s) in SUT cgroup during measured window "
                       f"(oom_events={window.oom_events})",
            )

        warm_path = artifact_dir / f"{lang}.warmup.json"
        warm.major_faults = faults["major_faults_delta"]
        warm.save(warm_path)

        # checksum the pulled artifacts (probe atomicity).
        checksums = paths.run_dir(self.run_id) / "CHECKSUMS.sha256"
        lines = []
        for p in (summary_path, p99_csv, cgroup_csv, cg_summary_path, cache_path, warm_path):
            rel = p.relative_to(paths.run_dir(self.run_id))
            lines.append(f"{_sha256(p)}  {rel}")
        with checksums.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        compose.compose(env_sut, ["stop", svc], check=False, timeout=120)

        rel_artifact = str(summary_path.relative_to(paths.run_dir(self.run_id)))
        return ProbeOutcome(
            achieved=report.rate, error_rate=report.error_rate,
            p99_ms=report.latency_p99_ns / 1e6, artifact=rel_artifact,
            per_second_p99=per_sec_values, under_warmed=warm.under_warmed, ok=True,
        )

    def _redis_keyspace_stats(self) -> dict[str, int | None]:
        """`redis-cli INFO stats` -> keyspace_hits / keyspace_misses (cumulative).

        Cheap (one exec) and taken only at window boundaries. Returns Nones when
        Redis is unreachable so the caller records "not measured".
        """
        res = compose.compose(
            self._env(),
            ["exec", "-T", compose.SVC_REDIS, "redis-cli", "INFO", "stats"],
            check=False, timeout=30,
        )
        if not res.ok:
            return {"keyspace_hits": None, "keyspace_misses": None}
        out: dict[str, int | None] = {"keyspace_hits": None, "keyspace_misses": None}
        for line in res.stdout.splitlines():
            key, _, value = line.partition(":")
            if key.strip() in out:
                try:
                    out[key.strip()] = int(value.strip())
                except ValueError:
                    pass
        return out

    def _scaled_min_calls(self) -> dict[str, int]:
        """spec warmup minimums scaled by the profile (smoke shrinks them)."""
        factor = self.profile.warmup_min_calls_factor
        return {
            name: max(1, int(round(minimum * factor)))
            for name, minimum in self.slo.warmup.per_endpoint_min_calls.items()
        }

    def _warmup_rate(self, min_calls: dict[str, int], cap_s: int) -> int:
        """Fixed sub-knee warmup rate at which the minimums are REACHABLE.

        The spec minimums are per-endpoint; an endpoint with mix share s and
        minimum m needs m/s total requests. The rate must deliver the worst-case
        endpoint within the hard cap (with 10% headroom): otherwise every probe
        is under-warmed by construction and the rung-emptying exclusion kills
        the run at the first rung. Floored at the old max(50, start/2) so the
        rate never drops below the prior behavior. Recorded per probe in
        WarmupResult.rate (full disclosure).
        """
        shares = {name: w / 100.0 for name, w in self.workload.mix.as_pairs()}
        required = 0.0
        for name, minimum in min_calls.items():
            share = shares.get(name, 0.0)
            if share > 0:
                required = max(required, minimum / share)
        floor = max(50, self.profile.ladder_start_rps // 2)
        if cap_s <= 0 or required <= 0:
            return floor
        return max(floor, int(required / (cap_s * 0.9)) + 1)

    def _warmup(self, env_load, lang, host_url, cap_s) -> WarmupResult:
        """Short sub-knee attack while polling /runtime-stats; flag under-warmed."""
        min_calls = self._scaled_min_calls()
        cfg: WarmupCfg = self.slo.warmup.model_copy(
            update={"per_endpoint_min_calls": min_calls}
        )
        poller = StatsPoller(host_url, hz=float(cfg.poll_hz or 1))
        rate = self._warmup_rate(min_calls, cap_s)
        state = WarmupState(cfg=cfg, language=lang, rate=rate)
        out_bin = f"/io/{lang}/warmup.bin"
        params = AttackParams(rate=rate, duration_s=cap_s,
                              max_workers=self.profile.max_workers,
                              connections=self.profile.connections,
                              timeout_s=self.profile.timeout_s)
        argv = vegeta.build_attack_argv(targets_in_container="targets.txt",
                                        out_bin_in_container=out_bin, params=params)
        poller.start()
        t0 = time.monotonic()
        compose.exec_in(env_load, compose.SVC_VEGETA,
                        ["sh", "-c", f"cd /io/{lang} && {' '.join(argv)}"], check=False)
        poller.stop()
        elapsed = time.monotonic() - t0
        for snap in poller.all():
            state.observe(snap)
        # Real latency-flatness input: per-second p99/p999 from the warmup .bin
        # (the gates would otherwise pass vacuously on empty series).
        try:
            qseries = vegeta.encode_per_second_quantiles(
                exec_prefix=[*env_load.base_argv(), "exec", "-T", compose.SVC_VEGETA],
                bin_in_container=out_bin, qs=(0.99, 0.999),
            )
            state.p99_series = [v[0] for _, v in qseries]
            state.p999_series = [v[1] for _, v in qseries]
        except (OSError, RuntimeError):
            pass  # gates fall back to the (empty-series) neutral result
        gates = state.evaluate()
        # Only ENFORCED gates decide under-warmed: gates spec/slo.yaml declares
        # but warmup.py does not implement are recorded (status
        # "not_implemented") and abstain; they must neither pass silently nor
        # flag every probe.
        under = not all(g.passed for g in gates if g.enforced)
        latest = poller.latest()
        return WarmupResult(
            language=lang, rate=rate, elapsed_seconds=elapsed,
            under_warmed=under and elapsed >= cap_s, gates=gates,
            final_db_pool_total=latest.stats.db_pool.total if latest else None,
            final_db_pool_max=latest.stats.db_pool.max if latest else None,
        )

    # ----- capacity gate (Linux) ------------------------------------------ #
    def measure_pg_create_knee(
        self, *, out_dir: Path, rungs: int, window_s: int, pg_cores: int | None = None
    ) -> capgate.PgKneeMeasurement:
        """Measure PG's create-tx ceiling via the create-only ladder (Linux).

        Brings up a SUT (go) + vegeta, drives POST /invoices at a geometric
        ladder, records EVERY rung, and hands the rung list to
        `capacity_gate.pg_knee_from_rungs`, which decides whether the ladder
        measured a knee at all. It no longer returns a bare float: a ladder that
        breaks on its first rung has measured ONE point, and the 2026-06-06 run
        published that single point as a "measured PG ceiling". Every
        early-return path below is a NOT-MEASURED outcome, not a fallback number.

        Per-rung PG cpu.stat deltas were descoped at scope freeze with the PG cgroup
        sampler: pg_cpu_delta_usec stays None permanently; a disclosed null,
        never a measurement. The rung table is persisted either way: INCLUDING the paths that
        never reach a rung, so `capacity-gate/pg_knee.json` always exists after
        this call and records why the ladder produced nothing.
        """
        cores = pg_cores if pg_cores is not None else capgate._BENCH_PG_CORES
        collected: list[capgate.RungResult] = []

        def _result(detail: str) -> capgate.PgKneeMeasurement:
            """Build the measurement AND persist it (every exit goes through here)."""
            out = capgate.pg_knee_from_rungs(collected, measured_at_cores=cores)
            if detail and not out.measured:
                out.detail = f"{out.detail}; {detail}"
            paths.write_text_atomic(
                out_dir / "pg_knee.json",
                json.dumps({
                    "knee_rps": out.knee_rps, "usable_rungs": out.usable_rungs,
                    "total_rungs": out.total_rungs, "measured_at_cores": cores,
                    "lower_bound": out.lower_bound, "detail": out.detail,
                    "rungs": out.rungs,
                }, indent=2),
            )
            return out

        env_sut = self._env(profiles=[compose.PROFILE_GO, compose.PROFILE_LOAD])
        up = compose.compose(env_sut, ["up", "-d", "--wait", compose.SVC_SUT_GO,
                                       compose.SVC_VEGETA], check=False, timeout=600)
        if not up.ok:
            return _result("SUT/vegeta failed to come up for the create-only ladder")
        sut.wait_ready(_HOST_URL_BY_LANG["go"], timeout_s=180.0)
        env_load = self._env(profiles=[compose.PROFILE_LOAD])
        # ship the create-only targets (already in out_dir from the phase).
        compose.exec_in(env_load, compose.SVC_VEGETA, ["sh", "-c", "mkdir -p /io/cap"], check=False)
        compose.compose(env_load, ["cp", str(out_dir) + "/.", f"{compose.SVC_VEGETA}:/io/cap/"],
                        check=False, timeout=600)
        rate = float(self.profile.ladder_start_rps)
        stop_reason = ""
        for _ in range(rungs):
            params = AttackParams(rate=int(rate), duration_s=window_s,
                                  max_workers=self.profile.max_workers,
                                  connections=self.profile.connections,
                                  timeout_s=self.profile.timeout_s)
            argv = vegeta.build_attack_argv(targets_in_container="targets.txt",
                                            out_bin_in_container="/io/cap/cap.bin", params=params)
            r = compose.exec_in(env_load, compose.SVC_VEGETA,
                                ["sh", "-c", f"cd /io/cap && {' '.join(argv)}"], check=False)
            if not r.ok:
                stop_reason = f"vegeta attack failed at rung {int(rate)} rps"
                break
            rep = compose.exec_in(env_load, compose.SVC_VEGETA,
                                  ["vegeta", "report", "-type=json", "/io/cap/cap.bin"], check=False)
            try:
                report = vegeta.parse_report_proc(rep)
            except RuntimeError as exc:
                stop_reason = f"vegeta report unparseable at rung {int(rate)} rps: {exc}"
                break
            collected.append(capgate.RungResult(
                rate=int(rate), achieved=report.rate,
                p99_ms=report.latency_p99_ns / 1e6, error_rate=report.error_rate,
            ))
            if (report.rate < rate * capgate.RUNG_TRACKING_TOLERANCE
                    or report.error_rate > capgate.RUNG_ERROR_MAX):
                stop_reason = f"rung {int(rate)} rps stopped tracking (PG ceiling)"
                break
            rate *= self.profile.ladder_step_factor
        compose.compose(env_sut, ["stop", compose.SVC_SUT_GO], check=False)
        return _result(stop_reason)

    # ----- equivalence (both SUTs up) ------------------------------------- #
    def run_equivalence(self) -> dict:
        """Run the existing equivalence pytest with both SUTs live.

        Brings both SUTs up under base compose (no resource conflict for the
        functional check), points the suite at their published ports via
        LEDGER_GO_URL/LEDGER_DOTNET_URL, runs it, tears the extra SUT down.
        """
        env = self._env(
            bench_overlay=False,
            profiles=[compose.PROFILE_GO, compose.PROFILE_DOTNET, compose.PROFILE_LOAD],
        )
        up = compose.compose(env, ["up", "-d", "--wait", compose.SVC_SUT_GO,
                                   compose.SVC_SUT_DOTNET, compose.SVC_VEGETA],
                             check=False, timeout=600)
        if not up.ok:
            return {"ok": False, "detail": f"up both SUTs failed: {up.stderr[-300:]}"}
        for url in _HOST_URL_BY_LANG.values():
            try:
                sut.wait_ready(url, timeout_s=180.0)
            except TimeoutError as exc:
                return {"ok": False, "detail": str(exc)}
        res = run(
            [sys.executable, "-m", "pytest", "validation/tests/test_equivalence.py",
             "-q", "--no-header"],
            check=False, timeout=900, cwd=str(paths.bench_dir()),
            extra_env={"LEDGER_GO_URL": _HOST_URL_BY_LANG["go"],
                       "LEDGER_DOTNET_URL": _HOST_URL_BY_LANG["dotnet"]},
        )
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        # stop the second SUT so only one is resident for the measured phases.
        compose.compose(env, ["stop", compose.SVC_SUT_DOTNET], check=False)
        compose.compose(env, ["stop", compose.SVC_SUT_GO], check=False)
        return {"ok": res.ok, "detail": tail[-300:]}

    # ----- build-block disclosure (core parallelism + data layer) --------- #
    def read_build(self, lang: str, extra_env: dict[str, str] | None = None):
        """Read the SUT's /runtime-stats build block (one up/poll/stop cycle).

        Used by _record_cell_knobs to populate (and on Linux assert) the pinned
        core count AND the effective data_layer for the cell. Brings the lang's
        SUT up under the bench overlay with the CELL's knob env (so cpuset,
        DATA_LAYER etc. are in effect exactly as the probes will see them),
        polls the build block once, and returns it (None if unavailable).
        """
        svc = _SVC_BY_LANG[lang]
        host_url = _HOST_URL_BY_LANG[lang]
        env_sut = self._env(profiles=[_PROFILE_BY_LANG[lang], compose.PROFILE_LOAD])
        env_sut.extra_env.update(extra_env or {})
        up = compose.compose(
            env_sut, ["up", "-d", "--wait", svc], check=False, timeout=600,
        )
        if not up.ok:
            return None
        try:
            sut.wait_ready(host_url, timeout_s=180.0)
            stats = fetch_once(host_url)
        except (TimeoutError, RuntimeError, OSError):
            return None
        finally:
            # Never leave the knob-read SUT resident on the SUT cpuset; the
            # next probe's fresh-SUT stop also covers this, but a probe of the
            # OTHER language would otherwise run with this one idling alongside.
            compose.compose(env_sut, ["stop", svc], check=False, timeout=120)
        return stats.build

    def read_processor_count(self, lang: str) -> int | None:
        build = self.read_build(lang)
        return None if build is None else build.processor_count
