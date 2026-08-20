"""Thin subprocess wrapper with consistent decoding and error surfacing.

KISS: synchronous subprocess.run only. The runner has no need for asyncio; the
one place a tight cadence matters (cgroups sampler) uses a dedicated thread, not
async I/O.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: list[str],
    *,
    check: bool = False,
    timeout: float | None = None,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> ProcResult:
    """Run `args`, capture text stdout/stderr.

    Never uses shell=True (avoids quoting drift across Windows/Linux). On
    `check=True` a non-zero exit raises RuntimeError with captured stderr.
    `extra_env` is merged over the inherited environment.
    """
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
        env=env,
        cwd=cwd,
    )
    result = ProcResult(
        args=list(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def which(name: str) -> str | None:
    """Locate an executable on PATH (cross-platform)."""
    import shutil

    return shutil.which(name)
