"""Preflight checks. Per-OS: Windows runs a subset.

Checks: docker present + daemon up; cgroup v2 unified (Linux only); free cores;
disk; binaries reachable (uv; vegeta lives in the container, checked on the
measured run); spec files parse. Emits `doctor.json` and a rich table. On
Windows the cgroup/host-tuning checks are skipped and reported as such.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from . import compose, config
from .util import paths
from .util.proc import run, which

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform.startswith("win")


class Check(BaseModel):
    name: str
    ok: bool
    skipped: bool = False
    detail: str = ""


class DoctorReport(BaseModel):
    platform: str
    checks: list[Check] = Field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok or c.skipped for c in self.checks)

    def save(self, path: Path) -> None:
        paths.write_text_atomic(path, self.model_dump_json(indent=2))


def _check_docker() -> Check:
    if which("docker") is None:
        return Check(name="docker_cli", ok=False, detail="docker not on PATH")
    if not compose.docker_available():
        return Check(name="docker_daemon", ok=False, detail="docker daemon not responding")
    return Check(name="docker_daemon", ok=True, detail="daemon reachable")


def _check_docker_native() -> Check:
    """Linux: the engine must run NATIVELY on this host, not in Docker Desktop's VM.

    Docker Desktop on Linux runs containers inside a linuxkit VM: container PIDs
    are invisible to the host, /sys/fs/cgroup has no container cgroups (the
    sampler that produces the HEADLINE CPU/memory numbers resolves nothing),
    cpusets pin to vCPUs instead of the tuned physical cores, and host tuning
    never reaches the VM scheduler. A measured run on Desktop produces
    plausible-looking latency numbers with NO resource ground truth.
    """
    if not IS_LINUX:
        return Check(name="docker_native_engine", ok=True, skipped=True, detail="non-Linux: skipped")
    res = run(["docker", "info", "--format", "{{.OperatingSystem}}|{{.KernelVersion}}"], timeout=15)
    if not res.ok:
        return Check(name="docker_native_engine", ok=False, detail="docker info failed")
    osname, _, kernel = res.stdout.strip().partition("|")
    if "Docker Desktop" in osname or "linuxkit" in kernel:
        return Check(
            name="docker_native_engine", ok=False,
            detail=f"engine is Docker Desktop (VM kernel {kernel}); measured runs "
                   "need the NATIVE engine (install docker-ce/moby-engine, "
                   "`docker context use default`)",
        )
    return Check(name="docker_native_engine", ok=True, detail=f"native engine on {osname}")


def _check_compose() -> Check:
    res = run(["docker", "compose", "version"], timeout=15)
    return Check(name="docker_compose", ok=res.ok, detail=res.stdout.strip() or res.stderr.strip())


def _check_cgroup_v2() -> Check:
    if not IS_LINUX:
        return Check(name="cgroup_v2_unified", ok=True, skipped=True, detail="non-Linux: skipped")
    marker = Path("/sys/fs/cgroup/cgroup.controllers")
    ok = marker.exists()
    return Check(name="cgroup_v2_unified", ok=ok, detail="unified present" if ok else "cgroup v2 unified not mounted")


def _check_free_cores() -> Check:
    import os

    n = os.cpu_count() or 0
    # Bench topology needs 6 SUT + 3 vegeta + 3 PG + 1 redis = 13 logical at least
    # on the Fedora box; Windows smoke just needs "some". Informational on win.
    if IS_WINDOWS:
        return Check(name="cpu_cores", ok=n >= 2, detail=f"{n} logical cores (smoke)")
    return Check(name="cpu_cores", ok=n >= 8, detail=f"{n} logical cores")


def _check_disk(min_gb: int = 20) -> Check:
    root = paths.repo_root()
    usage = shutil.disk_usage(str(root))
    free_gb = usage.free / (1024**3)
    return Check(name="disk_free", ok=free_gb >= min_gb, detail=f"{free_gb:.1f} GB free at {root}")


def _check_uv() -> Check:
    found = which("uv")
    if found is None:
        return Check(name="uv", ok=False, detail="uv not on PATH")
    res = run(["uv", "--version"], timeout=15)
    return Check(name="uv", ok=res.ok, detail=res.stdout.strip())


def _check_root_or_sudo() -> Check:
    """Linux: host-setup.sh --tune (re-run on every run-all start) needs root.

    Passes when running as root or when `sudo -n true` succeeds (NOPASSWD grant,
    a RUNBOOK prerequisite). Failing here keeps a measured run from dying at the
    host_setup phase after doctor already passed.
    """
    import os

    if not IS_LINUX:
        return Check(name="root_or_sudo", ok=True, skipped=True, detail="non-Linux: skipped")
    if os.geteuid() == 0:
        return Check(name="root_or_sudo", ok=True, detail="running as root")
    res = run(["sudo", "-n", "true"], timeout=15)
    return Check(
        name="root_or_sudo", ok=res.ok,
        detail="sudo -n ok (NOPASSWD)" if res.ok
        else "not root and `sudo -n true` failed; host tuning needs a NOPASSWD sudo grant",
    )


def _check_spec_parses() -> Check:
    try:
        config.load_workload()
        config.load_slo()
    except Exception as exc:  # noqa: BLE001 - surfaced to the report
        return Check(name="spec_parses", ok=False, detail=f"{type(exc).__name__}: {exc}")
    return Check(name="spec_parses", ok=True, detail="workload.yaml + slo.yaml valid")


def run_doctor(*, require_docker: bool = False) -> DoctorReport:
    """Run all checks. On Windows, docker checks are soft unless require_docker."""
    report = DoctorReport(platform=sys.platform)
    docker_check = _check_docker()
    if not require_docker and not docker_check.ok and IS_WINDOWS:
        docker_check = Check(name=docker_check.name, ok=True, skipped=True, detail="docker optional for non-run checks")
    report.checks.append(docker_check)
    if docker_check.ok and not docker_check.skipped:
        report.checks.append(_check_docker_native())
        report.checks.append(_check_compose())
    report.checks.append(_check_cgroup_v2())
    report.checks.append(_check_free_cores())
    report.checks.append(_check_disk())
    report.checks.append(_check_uv())
    report.checks.append(_check_root_or_sudo())
    report.checks.append(_check_spec_parses())
    return report
