"""One-SUT-at-a-time lifecycle: fresh container per probe.

Exactly one SUT profile (go XOR dotnet) is resident at a time so the two are
never co-resident. Every measured probe gets a FRESH SUT container, so warmup is
meaningful and prior-probe heap state never leaks across the pairing boundary.

This module holds the lifecycle interface + the healthcheck-wait loop driven
through compose.py. The phase runner sequences start -> warmup -> probe -> stop
per block.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import httpx

from . import compose
from .compose import ComposeEnv

Lang = Literal["go", "dotnet"]

_SVC_BY_LANG: dict[str, str] = {
    "go": compose.SVC_SUT_GO,
    "dotnet": compose.SVC_SUT_DOTNET,
}
_PROFILE_BY_LANG: dict[str, str] = {
    "go": compose.PROFILE_GO,
    "dotnet": compose.PROFILE_DOTNET,
}


@dataclass
class SutHandle:
    lang: Lang
    service: str
    base_url: str
    container_name: str


def env_for(lang: Lang, *, bench_overlay: bool = False, project_name: str = "ledgerline") -> ComposeEnv:
    """Compose env with exactly the one SUT profile + the load profile."""
    return ComposeEnv.base(
        bench_overlay=bench_overlay,
        profiles=[_PROFILE_BY_LANG[lang], compose.PROFILE_LOAD],
        project_name=project_name,
    )


def start_fresh(
    lang: Lang,
    *,
    base_url: str,
    bench_overlay: bool = False,
    project_name: str = "ledgerline",
    knob_env: dict[str, str] | None = None,
    readiness_timeout_s: float = 120.0,
) -> SutHandle:
    """Bring up postgres+redis+<one SUT>+vegeta fresh, wait for healthy.

    `knob_env` carries the levers (DB_POOL_MAX, GOGC, CACHE_TTL_SECONDS, ...)
    into the compose process environment. A fresh container is guaranteed by a
    prior `down` of the SUT service by the runner (recreate semantics).
    """
    env = env_for(lang, bench_overlay=bench_overlay, project_name=project_name)
    if knob_env:
        env.extra_env.update(knob_env)
    service = _SVC_BY_LANG[lang]
    # up --wait blocks on compose healthchecks; we still poll readyz for the app.
    compose.compose(env, ["up", "-d", "--wait"], check=True, timeout=readiness_timeout_s + 60)
    container = f"{project_name}-{service}-1"
    wait_ready(base_url, timeout_s=readiness_timeout_s)
    return SutHandle(lang=lang, service=service, base_url=base_url, container_name=container)


def stop(handle: SutHandle, *, bench_overlay: bool = False, project_name: str = "ledgerline", volumes: bool = False) -> None:
    """Stop the SUT (and shared services) for this profile."""
    env = env_for(handle.lang, bench_overlay=bench_overlay, project_name=project_name)
    compose.compose(env, ["down", *(["-v"] if volumes else [])], check=False)


def wait_ready(base_url: str, *, timeout_s: float = 120.0, interval_s: float = 0.5) -> None:
    """Poll GET /readyz until 200 (DB pool + Redis PING) or timeout.

    /readyz (not /healthz) is the app-level readiness gate: it checks the DB pool
    and Redis PING (spec/openapi.yaml). compose `up --wait` covers container
    health; this covers app readiness before warmup starts.
    """
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"{base_url}/readyz")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as exc:  # not ready yet
                last_err = exc
            time.sleep(interval_s)
    raise TimeoutError(
        f"SUT at {base_url} not ready within {timeout_s}s"
        + (f" (last error: {last_err})" if last_err else "")
    )
