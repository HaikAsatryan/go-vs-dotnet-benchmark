"""Repository path resolution and atomic write-temp-rename.

The runner is invoked from anywhere (PowerShell from bench/, make from root).
All spec/results paths are derived from the repo root, found by walking up from
this file until a directory containing both `spec/` and `infra/` is seen (both
are PERMANENT repo dirs, so root detection survives publish).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def repo_root() -> Path:
    """Walk up from this module until spec/ and infra/ are both present.

    (Sentinels must be PERMANENT repo dirs so root detection is stable across
    publish; transient working dirs must never be load-bearing here.)
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "spec").is_dir() and (parent / "infra").is_dir():
            return parent
    # Fallback: bench/ is two levels up from util/. Repo root is its parent.
    return here.parents[3]


def bench_dir() -> Path:
    return repo_root() / "bench"


def spec_dir() -> Path:
    return repo_root() / "spec"


def workload_path() -> Path:
    return spec_dir() / "workload.yaml"


def slo_path() -> Path:
    return spec_dir() / "slo.yaml"


def results_dir() -> Path:
    return repo_root() / "results"


def run_dir(run_id: str) -> Path:
    return results_dir() / run_id


def infra_dir() -> Path:
    return repo_root() / "infra"


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file + os.replace (atomic rename).

    Used by the run-ledger and any artifact that must never be observed
    half-written across a crash/resume boundary.
    """
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
