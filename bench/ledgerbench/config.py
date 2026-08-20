"""Load spec/workload.yaml + spec/slo.yaml into validated pydantic models.

These two files are config inputs to the runner (the services do NOT read them).
The models below are intentionally faithful to the YAML so the manifest can echo
every resolved value (full-disclosure manifest). Validation here catches a
malformed/edited spec early, before any container starts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .util import paths


# --------------------------------------------------------------------------- #
# workload.yaml
# --------------------------------------------------------------------------- #
class Mix(BaseModel):
    model_config = ConfigDict(extra="forbid")
    get_invoice: int
    list_invoices: int
    pricing_quote: int
    create_invoice: int
    pdf_stub: int
    healthz: int

    @model_validator(mode="after")
    def _sum_100(self) -> "Mix":
        total = (
            self.get_invoice
            + self.list_invoices
            + self.pricing_quote
            + self.create_invoice
            + self.pdf_stub
            + self.healthz
        )
        if total != 100:
            raise ValueError(f"mix percentages must sum to 100, got {total}")
        return self

    def as_pairs(self) -> list[tuple[str, int]]:
        """Stable (name, weight) order matching the workload file."""
        return [
            ("get_invoice", self.get_invoice),
            ("list_invoices", self.list_invoices),
            ("pricing_quote", self.pricing_quote),
            ("create_invoice", self.create_invoice),
            ("pdf_stub", self.pdf_stub),
            ("healthz", self.healthz),
        ]


class Zipf(BaseModel):
    model_config = ConfigDict(extra="ignore")
    alpha: float
    note: str | None = None


class Keyspace(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seeded_range: tuple[int, int]
    selection: str
    used_by: list[str] = Field(default_factory=list)
    runtime_created_ids: str | None = None


class Keyspaces(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customers: Keyspace
    invoices: Keyspace


class PageDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: dict[int, float]

    @model_validator(mode="after")
    def _check(self) -> "PageDistribution":
        if not self.weights:
            raise ValueError("page weights empty")
        s = sum(self.weights.values())
        if abs(s - 1.0) > 1e-6:
            raise ValueError(f"page weights must sum to 1.0, got {s}")
        return self

    def ordered(self) -> list[tuple[int, float]]:
        return sorted(self.weights.items())


class IntRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: int
    max: int

    @model_validator(mode="after")
    def _check(self) -> "IntRange":
        if self.min > self.max:
            raise ValueError(f"min {self.min} > max {self.max}")
        return self


class PayloadSpec(BaseModel):
    model_config = ConfigDict(extra="allow")  # fields block has nested shapes
    item_count: IntRange
    target_bytes: int
    fields: dict


class Payloads(BaseModel):
    model_config = ConfigDict(extra="forbid")
    create_invoice: PayloadSpec
    pricing_quote: PayloadSpec


class Rng(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_generator_seed_default: int
    algorithm: str


class CacheConstruction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target_hit_rate: float
    asserted_in_manifest: bool = True
    capacity_gate_lever: str | None = None


class Scale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customers: int
    invoices: int


class Seeding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seed: int
    scales: dict[str, Scale]
    items_per_invoice: IntRange


class Workload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mix: Mix
    zipf: Zipf
    keyspaces: Keyspaces
    page_distribution: PageDistribution
    payloads: Payloads
    rng: Rng
    cache_construction: CacheConstruction
    seeding: Seeding


# --------------------------------------------------------------------------- #
# slo.yaml
# --------------------------------------------------------------------------- #
class Slo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    metric: str
    threshold_ms: float
    applies_to: str
    error_rate_max: float


class Equivalence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    margin_pct: float
    decision_rule: str
    inside_margin: str
    outside_margin: str


class Ladder(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_rate_strategy: str | None = None
    start_rate_rps_default: int
    step_factor: float
    reps_per_rung: int
    confirm_reps: int
    window_seconds: int
    warmup_discard_seconds: int
    interleave: str
    knee_fit: str
    knee_ci: str


class WarmupCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hard_cap_seconds: int
    poll_hz: int
    per_endpoint_min_calls: dict[str, int]
    gates: dict[str, bool]
    note: str | None = None


class CoValidation(BaseModel):
    # Trimmed at scope freeze: the oha tolerance / toxiproxy sustained-degradation /
    # self-saturation fields described bench-tier checks that were descoped.
    model_config = ConfigDict(extra="ignore")
    achieved_vs_offered_tolerance_pct: float
    stall_test: dict


class CapacityLever(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    name: str
    # The two numeric levers carry from/to; the core-move lever adds taken_from.
    from_: float | int | None = Field(default=None, alias="from")
    to: float | int | None = None
    taken_from: str | None = None


class CapacityGate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headroom_factor: float
    levers_in_order: list[CapacityLever]
    never: str | None = None


class SloConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slo: Slo
    equivalence: Equivalence
    ladder: Ladder
    warmup: WarmupCfg
    co_validation: CoValidation
    capacity_gate: CapacityGate
    # stationarity / soak are read by the runner and analysis stages; kept as
    # raw dicts here so the runner core stays lean but the values are still echoed.
    # (hysteresis was descoped at scope freeze with the descending ladder pass; the
    # block is gone from spec/slo.yaml and the field was removed with it.)
    stationarity: dict | None = None
    soak: dict | None = None


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_workload(path: Path | None = None) -> Workload:
    return Workload.model_validate(_load_yaml(path or paths.workload_path()))


def load_slo(path: Path | None = None) -> SloConfig:
    return SloConfig.model_validate(_load_yaml(path or paths.slo_path()))


def config_hash(spec_dir: Path | None = None) -> str:
    """SHA-256 over every file under spec/ (recursively).

    Goes into the run-ledger/manifest so a resumed run can detect a spec edit.
    The docs promise "a hash of spec/", so this covers the WHOLE spec tree
    (workload.yaml, slo.yaml, schema.sql, openapi.yaml, postgres/*, the .md
    contracts, etc.), not just the two YAMLs. Each entry mixes in the file's
    sorted POSIX-relative path then its raw bytes, so a rename, a content change,
    or a comment edit all change the hash; sorting makes it order-stable.

    Sorting is on the POSIX-relative path SPLIT INTO PARTS, never on the Path
    object: `sorted()` over `WindowsPath` compares case-insensitively, which
    reorders `postgres/README.md` against `postgres/initdb/...` and yields a
    different digest on Windows than on Linux for a byte-identical tree. That
    would make a published hash unverifiable off-Linux and would break `--resume`
    across platforms. Splitting on "/" reproduces PosixPath's part-wise ordering
    on every platform.
    """
    root = spec_dir or paths.spec_dir()
    h = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix().split("/"),
    )
    for p in files:
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()
