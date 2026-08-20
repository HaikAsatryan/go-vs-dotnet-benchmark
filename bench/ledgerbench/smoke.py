"""Smoke entry (Windows-ok). Functional only, resource accounting OFF.

The flow:
  1. build sut-go, sut-dotnet, vegeta, seed (base compose, NO resource overlay).
  2. seed smoke scale via the seed container (applies spec/schema.sql, grants,
     COPY-loads seed-42 data, restarts sequences, runs invariants).
  3. up BOTH SUTs + vegeta together (base compose; cross-service equivalence
     needs both live against the same DB) and poll /readyz on the published
     ports.
  4. equivalence: bench/validation pytest with LEDGER_GO_URL/LEDGER_DOTNET_URL.
  5. CO stall sanity: compose pause sut-go 1 s, unpause, /healthz must answer.
  6. per-SUT 30 s probe at low rate via the vegeta container (in-network URLs,
     smoke Zipf seed); assert 2xx + achieved ~= offered. Latency numbers are
     smoke-only, never measurement-grade.
  7. teardown: down -v.

Resource accounting is OFF (cgroups.py records "disabled (non-Linux)" on
Windows). Output is labeled smoke-only.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import compose, vegeta
from .compose import ComposeEnv
from .config import Workload, load_workload
from .util import paths
from .util.clock import rfc3339_micros
from .util.proc import run

SMOKE_SCALE = "smoke"
SMOKE_PROBE_RATE = 50          # low fixed rate
SMOKE_PROBE_DURATION_S = 30
SMOKE_TARGET_COUNT = 5000      # pre-expanded targets list for the 30 s probe
SMOKE_ACHIEVED_TOLERANCE_PCT = 5.0   # relaxed vs slo co_validation 2% (smoke-only label)
SMOKE_MIN_SUCCESS_RATIO = 0.999

GO_URL_ENV = "LEDGER_GO_URL"
DOTNET_URL_ENV = "LEDGER_DOTNET_URL"
GO_BASE_URL = "http://localhost:8081"
DOTNET_BASE_URL = "http://localhost:8082"

SUTS = (
    ("go", compose.SVC_SUT_GO, GO_BASE_URL, "http://sut-go:8080"),
    ("dotnet", compose.SVC_SUT_DOTNET, DOTNET_BASE_URL, "http://sut-dotnet:8080"),
)


@dataclass
class SmokeStep:
    name: str
    ok: bool
    detail: str = ""
    deferred: bool = False


@dataclass
class SmokeReport:
    label: str = "smoke-only"
    resource_accounting: str = "disabled (non-Linux)" if not sys.platform.startswith("linux") else "off (smoke)"
    started_utc: str = field(default_factory=rfc3339_micros)
    steps: list[SmokeStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps if not s.deferred)


def smoke_targets(
    wl: Workload, *, out_dir: Path, seed: int, base_url: str, scale: str = SMOKE_SCALE
) -> vegeta.GeneratedTargets:
    """Generate probe targets with the smoke Zipf seed and an in-network base URL."""
    return vegeta.generate_targets(
        wl,
        out_dir=out_dir,
        count=SMOKE_TARGET_COUNT,
        seed=seed,
        scale=scale,
        base_url=base_url,
    )


def build_argv() -> list[str]:
    env = ComposeEnv.base()
    return compose.build_argv(
        env, [compose.SVC_SUT_GO, compose.SVC_SUT_DOTNET, compose.SVC_VEGETA, compose.SVC_SEED]
    )


def seed_argv(*, scale: str = SMOKE_SCALE, seed: int = 42) -> list[str]:
    """One-shot seeder: schema + grants + truncate + COPY + invariants."""
    env = ComposeEnv.base(profiles=[compose.PROFILE_SEED])
    return compose.run_oneshot_argv(
        env,
        compose.SVC_SEED,
        [
            "--dsn", "postgres://postgres:postgres@postgres:5432/ledgerline",
            "--scale", scale,
            "--seed", str(seed),
            "--schema", "/spec/schema.sql",
            "--grants",
            "--truncate",
            "--mode", "copy",
        ],
    )


def up_argv() -> list[str]:
    env = ComposeEnv.base(
        profiles=[compose.PROFILE_GO, compose.PROFILE_DOTNET, compose.PROFILE_LOAD]
    )
    return compose.up_wait_argv(
        env, [compose.SVC_SUT_GO, compose.SVC_SUT_DOTNET, compose.SVC_VEGETA]
    )


def teardown_argv() -> list[str]:
    env = ComposeEnv.base(
        profiles=[compose.PROFILE_GO, compose.PROFILE_DOTNET, compose.PROFILE_LOAD, compose.PROFILE_SEED]
    )
    return compose.down_argv(env, volumes=True)


def equivalence_argv() -> list[str]:
    return [sys.executable, "-m", "pytest", "validation/tests", "-q", "--no-header"]


def plan() -> list[str]:
    """Human-readable ordered command plan for the smoke flow (no execution)."""
    return [
        " ".join(build_argv()),
        " ".join(seed_argv()),
        " ".join(up_argv()),
        f"# poll /readyz on {GO_BASE_URL} + {DOTNET_BASE_URL}",
        f"# {' '.join(equivalence_argv())}  (env {GO_URL_ENV}/{DOTNET_URL_ENV})",
        "# compose pause sut-go ; sleep 1 ; unpause ; /healthz check",
        f"# per-SUT: vegeta attack @{SMOKE_PROBE_RATE} rps x {SMOKE_PROBE_DURATION_S}s via /io/<lang> (in-network)",
        " ".join(teardown_argv()),
    ]


def wait_ready(base_url: str, *, timeout_s: float = 120.0) -> bool:
    """Poll /readyz until 200 (the SUT images carry no container healthcheck)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/readyz", timeout=3.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def _probe(report: SmokeReport, wl: Workload, lang: str, net_url: str, io_dir: Path, seed: int) -> None:
    """Generate targets, ship them into the vegeta container, attack, assert."""
    env = ComposeEnv.base(profiles=[compose.PROFILE_LOAD])
    smoke_targets(wl, out_dir=io_dir / lang, seed=seed, base_url=net_url)

    res = compose.exec_in(env, compose.SVC_VEGETA, ["sh", "-c", f"mkdir -p /io/{lang}"], check=False)
    cp = compose.compose(
        env, ["cp", str(io_dir / lang) + "/.", f"{compose.SVC_VEGETA}:/io/{lang}/"], check=False, timeout=300
    )
    if not (res.ok and cp.ok):
        report.steps.append(SmokeStep(name=f"probe_{lang}_ship", ok=False, detail=(res.stderr + cp.stderr)[-400:]))
        return

    params = vegeta.AttackParams(
        rate=SMOKE_PROBE_RATE,
        duration_s=SMOKE_PROBE_DURATION_S,
        max_workers=64,
        connections=16,
        timeout_s=5.0,
    )
    attack = vegeta.build_attack_argv(
        targets_in_container="targets.txt",
        out_bin_in_container=f"/io/{lang}/out.bin",
        params=params,
    )
    res = compose.exec_in(
        env, compose.SVC_VEGETA, ["sh", "-c", f"cd /io/{lang} && {' '.join(attack)}"], check=False
    )
    if not res.ok:
        report.steps.append(SmokeStep(name=f"probe_{lang}_attack", ok=False, detail=res.stderr[-400:]))
        return

    rep_proc = compose.exec_in(
        env, compose.SVC_VEGETA, ["vegeta", "report", "-type=json", f"/io/{lang}/out.bin"], check=False
    )
    try:
        rep = vegeta.parse_report_proc(rep_proc)
    except RuntimeError as exc:
        report.steps.append(SmokeStep(name=f"probe_{lang}_report", ok=False, detail=str(exc)[:400]))
        return

    rate_ok, ratio = vegeta.assert_achieved_vs_offered(rep, SMOKE_PROBE_RATE, SMOKE_ACHIEVED_TOLERANCE_PCT)
    success_ok = rep.success_ratio >= SMOKE_MIN_SUCCESS_RATIO
    detail = (
        f"[smoke-only, not measurement-grade] achieved/offered={ratio:.3f} "
        f"success={rep.success_ratio:.4f} codes={rep.status_codes} "
        f"p50={rep.latency_p50_ns / 1e6:.1f}ms p99={rep.latency_p99_ns / 1e6:.1f}ms"
        + (f" errors={rep.errors[:3]}" if rep.errors else "")
    )
    report.steps.append(SmokeStep(name=f"probe_{lang}", ok=rate_ok and success_ok, detail=detail))


def run_smoke(*, dry_run: bool = False, seed: int | None = None) -> SmokeReport:
    """Execute (or dry-run) the smoke flow."""
    wl = load_workload()
    if seed is None:
        seed = wl.rng.target_generator_seed_default
    report = SmokeReport()

    if dry_run:
        for line in plan():
            report.steps.append(SmokeStep(name="plan", ok=True, detail=line))
        return report

    if not compose.docker_available():
        report.steps.append(
            SmokeStep(name="docker", ok=False, detail="docker daemon not available; cannot run smoke")
        )
        return report

    try:
        # 1. build
        res = compose.run(build_argv(), check=False, timeout=3600)
        report.steps.append(SmokeStep(name="build", ok=res.ok, detail=res.stderr.strip()[-500:]))
        if not res.ok:
            return report

        # 2. seed smoke + invariants (the seeder runs invariants itself)
        res = compose.run(seed_argv(seed=wl.seeding.seed), check=False, timeout=900)
        report.steps.append(SmokeStep(name="seed_smoke", ok=res.ok, detail=(res.stdout + res.stderr).strip()[-500:]))
        if not res.ok:
            return report

        # 3. up both SUTs + vegeta; poll /readyz
        res = compose.run(up_argv(), check=False, timeout=600)
        report.steps.append(SmokeStep(name="up", ok=res.ok, detail=res.stderr.strip()[-500:]))
        if not res.ok:
            return report
        for lang, _svc, base_url, _net in SUTS:
            ok = wait_ready(base_url)
            report.steps.append(SmokeStep(name=f"ready_{lang}", ok=ok, detail=base_url))
        if not report.ok:
            return report

        # 4. equivalence (both SUTs live against the same smoke DB)
        res = run(
            equivalence_argv(),
            check=False,
            timeout=900,
            cwd=str(paths.bench_dir()),
            extra_env={GO_URL_ENV: GO_BASE_URL, DOTNET_URL_ENV: DOTNET_BASE_URL},
        )
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        report.steps.append(SmokeStep(name="equivalence", ok=res.ok, detail=tail[-400:]))

        # 5. CO stall sanity (docker pause once; this stall check plus per-probe
        #    achieved-vs-offered IS the CO evidence; the once-planned full CO
        #    suite was descoped at scope freeze)
        env_go = ComposeEnv.base(profiles=[compose.PROFILE_GO])
        paused = compose.compose(env_go, ["pause", compose.SVC_SUT_GO], check=False)
        time.sleep(1.0)
        unpaused = compose.compose(env_go, ["unpause", compose.SVC_SUT_GO], check=False)
        healthz_ok = False
        if paused.ok and unpaused.ok:
            try:
                healthz_ok = httpx.get(f"{GO_BASE_URL}/healthz", timeout=10.0).status_code == 200
            except httpx.HTTPError:
                healthz_ok = False
        report.steps.append(SmokeStep(name="co_stall", ok=paused.ok and unpaused.ok and healthz_ok))

        # 6. per-SUT 30 s probe (in-network URLs through the vegeta container)
        io_dir = paths.run_dir(f"smoke-{rfc3339_micros().replace(':', '').replace('.', '')}") / "io"
        for lang, _svc, _base, net_url in SUTS:
            _probe(report, wl, lang, net_url, io_dir, seed)
    finally:
        # 7. teardown (always; volumes dropped for a clean next run)
        res = compose.run(teardown_argv(), check=False, timeout=600)
        report.steps.append(SmokeStep(name="teardown", ok=res.ok))
    return report
