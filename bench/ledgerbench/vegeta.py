"""Vegeta target/body generation + attack invocation + report parsing.

Implements spec/workload.yaml EXACTLY and deterministically given the
`target_generator_seed`:
  - request mix (30 get / 25 list / 20 quote / 15 create / 8 pdf / 2 healthz),
  - finite inverse-CDF Zipf(alpha=1.0) id selection (zipf.py),
  - page distribution (1..5 skewed to 1),
  - create/quote body generation to spec sizes (~2 KB / ~8 KB), int64 minor
    units, no floats anywhere in money paths.

Output: a vegeta targets file (one request per line: `METHOD URL`, optional
`@bodyfile` + headers) plus the referenced body files, all under an io dir
shared with the vegeta container (the `bench-io` volume).

Attack invocation is via `docker exec` into the running vegeta container, so
attack params are runner-controlled, not baked into the image.
The `.bin` is collected and `vegeta report -type=json` parsed; achieved-vs-offered
is asserted within spec/slo.yaml co_validation tolerance.

Determinism note: the targets file is a pure function of (seed, count, scale,
mix, distributions). Two runs with the same seed produce byte-identical files.
See bench/NOTES.md ("PRNG" and "Body sizing") for the PRNG and body-sizing decisions.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Workload
from .util import paths
from .util.pcg64 import PCG64
from .util.proc import ProcResult, run
from .zipf import ZipfTable, build_zipf_table

# Frozen route templates (match spec/openapi.yaml + spec/runtime-stats.md keys).
ENDPOINTS = {
    "get_invoice": ("GET", "/invoices/{id}"),
    "list_invoices": ("GET", "/customers/{id}/invoices"),
    "pricing_quote": ("POST", "/pricing/quote"),
    "create_invoice": ("POST", "/invoices"),
    "pdf_stub": ("POST", "/invoices/{id}/pdf-stub"),
    "healthz": ("GET", "/healthz"),
}

# Filler char for create-invoice descriptions (printable ASCII, 1 byte, no JSON
# escaping needed). Padding stays within CreateInvoiceItem.description maxLength.
_FILLER_CHAR = "x"
_DESCRIPTION_MAX = 200  # openapi CreateInvoiceItem.description maxLength


@dataclass
class ScaleRanges:
    """Resolved keyspace ranges for the active scale (full vs smoke)."""

    customer_max: int
    invoice_max: int

    @classmethod
    def from_workload(cls, wl: Workload, scale: str) -> "ScaleRanges":
        sc = wl.seeding.scales[scale]
        return cls(customer_max=sc.customers, invoice_max=sc.invoices)


@dataclass
class GeneratedTargets:
    """Result of a targets-file generation pass."""

    targets_path: Path
    bodies_dir: Path
    count: int
    seed: int
    scale: str
    # Per-endpoint emitted counts (for mix-proportion assertion + manifest echo).
    endpoint_counts: dict[str, int] = field(default_factory=dict)
    # Body size stats for the size-tolerance assertion.
    create_body_bytes: list[int] = field(default_factory=list)
    quote_body_bytes: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# mix expansion
# --------------------------------------------------------------------------- #
def build_mix_cdf(wl: Workload) -> tuple[list[str], list[float]]:
    """Cumulative-weight table over endpoint names in workload order."""
    pairs = wl.mix.as_pairs()
    names = [name for name, _ in pairs]
    total = sum(w for _, w in pairs)
    cdf: list[float] = []
    running = 0
    for _, w in pairs:
        running += w
        cdf.append(running / total)
    cdf[-1] = 1.0
    return names, cdf


def build_page_cdf(wl: Workload) -> tuple[list[int], list[float]]:
    """Cumulative table over pages 1..5 from the page_distribution weights."""
    ordered = wl.page_distribution.ordered()
    pages = [p for p, _ in ordered]
    cdf: list[float] = []
    running = 0.0
    for _, w in ordered:
        running += w
        cdf.append(running)
    cdf[-1] = 1.0
    return pages, cdf


# --------------------------------------------------------------------------- #
# sku / body generation
# --------------------------------------------------------------------------- #
def _sku_categories(field_spec: dict) -> list[str]:
    cats = field_spec.get("sku", {}).get("categories")
    if not cats:
        raise ValueError("workload sku field missing categories")
    return list(cats)


def gen_sku(rng: PCG64, categories: list[str]) -> str:
    """`<CAT>-<NNNN>` matching ^[A-Z]{2}-[0-9]{4}$ (openapi sku pattern)."""
    cat = categories[rng.randint(0, len(categories) - 1)]
    nnnn = rng.randint(0, 9999)
    return f"{cat}-{nnnn:04d}"


def gen_create_body(rng: PCG64, wl: Workload, customer_id: int) -> str:
    """Deterministic CreateInvoiceRequest JSON near payloads.create_invoice.target_bytes.

    Uses compact JSON; the per-item `description` is the size knob (filler:true).

    Spec tension (bench/NOTES.md ("Body sizing vs the wire schema")): item_count is uniform [3,8] but description is
    capped at maxLength 200, so a 3-item body cannot reach target-512 (~1536 B)
    even fully padded. Since item count does NOT cause cross-language divergence
    (both services just parse N items), we draw n uniformly and then GROW it
    toward maxItems=8 only as far as needed for the body to be able to reach the
    tolerance band; then we pad descriptions to land near target. Deterministic:
    the growth and padding consume no RNG.
    """
    spec = wl.payloads.create_invoice
    cats = _sku_categories(spec.fields)
    tol = 512  # spec asserts target +/- 512
    n_items = rng.randint(spec.item_count.min, spec.item_count.max)
    status = "draft" if rng.random() < 0.5 else "open"  # weights 0.5/0.5
    target = spec.target_bytes

    items: list[dict] = []
    for _ in range(n_items):
        items.append(
            {
                "sku": gen_sku(rng, cats),
                "description": "",  # filled below
                "qty": rng.randint(1, 50),
                "unit_price_minor": rng.randint(100, 5_000_000),
            }
        )

    body = {
        "customer_id": customer_id,
        "currency": "USD",
        "status": status,
        "items": items,
    }

    def maxed_size() -> int:
        for it in items:
            it["description"] = _FILLER_CHAR * _DESCRIPTION_MAX
        return len(_compact(body).encode("utf-8"))

    # Grow item count toward maxItems until the band is reachable (or capped).
    while maxed_size() < target - tol and len(items) < spec.item_count.max:
        items.append(
            {
                "sku": gen_sku(rng, cats),
                "description": _FILLER_CHAR * _DESCRIPTION_MAX,
                "qty": rng.randint(1, 50),
                "unit_price_minor": rng.randint(100, 5_000_000),
            }
        )

    # Reset descriptions to empty, then pad evenly to approach target. We solve
    # for the per-item filler length that minimizes |size - target| without
    # exceeding maxLength; a short bisection on length is exact and deterministic.
    for it in items:
        it["description"] = ""
    empty_size = len(_compact(body).encode("utf-8"))
    n = len(items)
    # Each filler char adds exactly 1 byte/item (ASCII, no JSON escaping).
    needed = target - empty_size
    per_item = 0 if needed <= 0 else min(_DESCRIPTION_MAX, (needed + n - 1) // n)
    per_item = max(1, per_item)  # minLength 1
    for it in items:
        it["description"] = _FILLER_CHAR * per_item

    # If padding overshot the upper bound, trim one char at a time (cheap, n<=8).
    while len(_compact(body).encode("utf-8")) > target + tol and per_item > 1:
        per_item -= 1
        for it in items:
            it["description"] = _FILLER_CHAR * per_item

    return _compact(body)


def gen_quote_body(rng: PCG64, wl: Workload) -> str:
    """Deterministic QuoteRequest JSON near payloads.pricing_quote.target_bytes.

    QuoteItem has no description field (openapi), so the only schema-faithful way
    to approach the 8 KB target with exactly 50 compact items is whitespace: we
    serialize with indentation. Both services discard insignificant JSON
    whitespace identically on parse, so this introduces zero behavioral skew.
    See bench/NOTES.md ("Body sizing vs the wire schema").
    """
    spec = wl.payloads.pricing_quote
    cats = _sku_categories(spec.fields)
    n_items = rng.randint(spec.item_count.min, spec.item_count.max)  # 50..50

    items = [
        {
            "sku": gen_sku(rng, cats),
            "qty": rng.randint(1, 200),
            "unit_price_minor": rng.randint(100, 1_000_000),
        }
        for _ in range(n_items)
    ]
    body = {"currency": "USD", "items": items}

    target = spec.target_bytes
    # Pick the indent that lands closest to target without exceeding +tolerance.
    best = _compact(body)
    best_size = len(best.encode("utf-8"))
    for indent in range(1, 12):
        candidate = json.dumps(body, separators=(",", ": "), indent=indent)
        size = len(candidate.encode("utf-8"))
        if abs(size - target) < abs(best_size - target):
            best, best_size = candidate, size
        if size >= target:
            break
    return best


def _compact(obj: object) -> str:
    """Compact JSON: no spaces, sorted-free (insertion order), UTF-8."""
    return json.dumps(obj, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# targets-file generation
# --------------------------------------------------------------------------- #
def generate_targets(
    wl: Workload,
    *,
    out_dir: Path,
    count: int,
    seed: int,
    scale: str,
    base_url: str = "",
    customer_zipf: ZipfTable | None = None,
    invoice_zipf: ZipfTable | None = None,
) -> GeneratedTargets:
    """Emit a vegeta targets file + body files. Pure function of inputs.

    `base_url` is normally empty so the targets file holds path-only URLs and
    vegeta is invoked with the SUT base URL; absolute URLs are supported for
    direct/smoke use. Body files are written once and referenced by `@path`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    bodies_dir = out_dir / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)

    ranges = ScaleRanges.from_workload(wl, scale)
    if customer_zipf is None:
        customer_zipf = build_zipf_table(ranges.customer_max, wl.zipf.alpha)
    if invoice_zipf is None:
        invoice_zipf = build_zipf_table(ranges.invoice_max, wl.zipf.alpha)

    mix_names, mix_cdf = build_mix_cdf(wl)
    pages, page_cdf = build_page_cdf(wl)

    rng = PCG64(seed)
    lines: list[str] = []
    endpoint_counts: dict[str, int] = {name: 0 for name in mix_names}
    create_sizes: list[int] = []
    quote_sizes: list[int] = []
    body_seq = 0

    for _ in range(count):
        name = mix_names[rng.choice_index(mix_cdf)]
        endpoint_counts[name] += 1
        method, template = ENDPOINTS[name]

        if name == "get_invoice":
            inv_id = invoice_zipf.sample_id(rng)
            lines.append(f"{method} {base_url}/invoices/{inv_id}")
        elif name == "pdf_stub":
            inv_id = invoice_zipf.sample_id(rng)
            lines.append(f"{method} {base_url}/invoices/{inv_id}/pdf-stub")
        elif name == "list_invoices":
            cust_id = customer_zipf.sample_id(rng)
            page = pages[rng.choice_index(page_cdf)]
            lines.append(
                f"{method} {base_url}/customers/{cust_id}/invoices?page={page}"
            )
        elif name == "healthz":
            lines.append(f"{method} {base_url}/healthz")
        elif name == "create_invoice":
            cust_id = customer_zipf.sample_id(rng)
            body = gen_create_body(rng, wl, cust_id)
            create_sizes.append(len(body.encode("utf-8")))
            body_name = f"create_{body_seq:08d}.json"
            paths.write_text_atomic(bodies_dir / body_name, body)
            body_seq += 1
            lines.append(f"{method} {base_url}/invoices")
            lines.append("Content-Type: application/json")
            lines.append(f"@{_io_body_ref(body_name)}")
        elif name == "pricing_quote":
            body = gen_quote_body(rng, wl)
            quote_sizes.append(len(body.encode("utf-8")))
            body_name = f"quote_{body_seq:08d}.json"
            paths.write_text_atomic(bodies_dir / body_name, body)
            body_seq += 1
            lines.append(f"{method} {base_url}/pricing/quote")
            lines.append("Content-Type: application/json")
            lines.append(f"@{_io_body_ref(body_name)}")
        else:  # pragma: no cover - mix is exhaustive
            raise ValueError(f"unknown endpoint {name}")

        lines.append("")  # blank line separates vegeta targets

    targets_path = out_dir / "targets.txt"
    paths.write_text_atomic(targets_path, "\n".join(lines) + "\n")

    return GeneratedTargets(
        targets_path=targets_path,
        bodies_dir=bodies_dir,
        count=count,
        seed=seed,
        scale=scale,
        endpoint_counts=endpoint_counts,
        create_body_bytes=create_sizes,
        quote_body_bytes=quote_sizes,
    )


def _io_body_ref(body_name: str) -> str:
    """Reference written into the targets file for an `@bodyfile` line.

    Emitted as a STABLE RELATIVE path (`bodies/<name>`) so the targets file is
    byte-deterministic and portable: vegeta resolves `@` relative to its working
    directory, so the runner/exec sets cwd (or the container mounts the io dir)
    to the targets-file directory. This keeps the targets file independent of the
    host temp path -> reproducible given the seed (bench/NOTES.md ("PRNG: hand-rolled PCG64")).
    """
    return f"bodies/{body_name}"


# --------------------------------------------------------------------------- #
# attack invocation + report parsing
# --------------------------------------------------------------------------- #
@dataclass
class AttackParams:
    rate: int
    duration_s: int
    max_workers: int
    connections: int
    timeout_s: float
    keepalive: bool = True


@dataclass
class AttackResult:
    bin_path: Path
    report: "VegetaReport"
    params: AttackParams


@dataclass
class VegetaReport:
    """Parsed `vegeta report -type=json` summary (subset we record)."""

    requests: int
    rate: float          # achieved request rate (mean)
    throughput: float    # successful responses / second
    duration_ns: int
    success_ratio: float
    status_codes: dict[str, int]
    latency_mean_ns: int
    latency_p50_ns: int
    latency_p99_ns: int
    latency_max_ns: int
    errors: list[str]

    @property
    def error_rate(self) -> float:
        return 1.0 - self.success_ratio

    @classmethod
    def from_json(cls, text: str) -> "VegetaReport":
        d = json.loads(text)
        lat = d.get("latencies", {})
        return cls(
            requests=int(d.get("requests", 0)),
            rate=float(d.get("rate", 0.0)),
            throughput=float(d.get("throughput", 0.0)),
            duration_ns=int(d.get("duration", 0)),
            success_ratio=float(d.get("success", 0.0)),
            status_codes={str(k): int(v) for k, v in d.get("status_codes", {}).items()},
            latency_mean_ns=int(lat.get("mean", 0)),
            latency_p50_ns=int(lat.get("50th", 0)),
            latency_p99_ns=int(lat.get("99th", 0)),
            latency_max_ns=int(lat.get("max", 0)),
            errors=list(d.get("errors", []) or []),
        )


def assert_achieved_vs_offered(
    report: VegetaReport, offered_rate: float, tolerance_pct: float
) -> tuple[bool, float]:
    """Check achieved rate is within `tolerance_pct` of offered (slo co_validation).

    Returns (ok, ratio). Ratio = achieved/offered. The runner marks a probe
    invalid (not silently retried) when this fails; reporting is the caller's.
    """
    if offered_rate <= 0:
        raise ValueError("offered_rate must be > 0")
    ratio = report.rate / offered_rate
    ok = abs(ratio - 1.0) * 100.0 <= tolerance_pct
    return ok, ratio


def build_attack_argv(
    *,
    targets_in_container: str,
    out_bin_in_container: str,
    params: AttackParams,
) -> list[str]:
    """Argv for `vegeta attack` (container-relative paths).

    The caller wraps this with `docker exec <vegeta> sh -c '<...> > out.bin'`
    (compose.py). Attack params are pinned + echoed.
    """
    argv = [
        "vegeta",
        "attack",
        f"-targets={targets_in_container}",
        f"-rate={params.rate}",
        f"-duration={params.duration_s}s",
        f"-max-workers={params.max_workers}",
        f"-connections={params.connections}",
        f"-timeout={params.timeout_s}s",
        f"-keepalive={'true' if params.keepalive else 'false'}",
        f"-output={out_bin_in_container}",
    ]
    return argv


def parse_report_proc(result: ProcResult) -> VegetaReport:
    """Parse a completed `vegeta report -type=json` invocation."""
    if not result.ok:
        raise RuntimeError(f"vegeta report failed: {result.stderr.strip()}")
    return VegetaReport.from_json(result.stdout)


def run_report_json(
    *, exec_prefix: list[str], bin_in_container: str
) -> VegetaReport:
    """Invoke `vegeta report -type=json` over a .bin via an exec prefix.

    `exec_prefix` is e.g. ["docker","exec","ledger-vegeta"]; kept injectable so
    tests pass a no-op prefix and integration passes the real one.
    """
    argv = [*exec_prefix, "vegeta", "report", "-type=json", bin_in_container]
    return parse_report_proc(run(argv))


# --------------------------------------------------------------------------- #
# per-second p99 series (stationarity input for the fit step)
# --------------------------------------------------------------------------- #
def _percentile_ns(sorted_ns: list[int], q: float) -> int:
    """Nearest-rank percentile of an ascending int list (q in [0,1])."""
    if not sorted_ns:
        return 0
    idx = min(len(sorted_ns) - 1, int(q * (len(sorted_ns) - 1) + 0.5))
    return sorted_ns[idx]


def per_second_quantiles_from_ndjson(
    lines: "Iterable[str]", *, qs: tuple[float, ...] = (0.99,)
) -> list[tuple[int, tuple[float, ...]]]:
    """Bucket vegeta `encode --to json` NDJSON into per-second quantiles (ms).

    Each NDJSON record carries `timestamp` (RFC3339) and `latency` (ns). We bucket
    by integer second RELATIVE to the first record's epoch second and emit
    (second_index, (q_ms, ...)) ascending: one value per requested quantile.
    MEMORY-BOUNDED: latencies are accumulated per active second only; once the
    second advances we flush that bucket's quantiles and drop its samples.
    Records are time-ordered in a vegeta .bin, so a single-pass streaming
    bucketer is exact.
    """
    out: list[tuple[int, tuple[float, ...]]] = []
    base_sec: int | None = None
    cur_sec: int | None = None
    bucket: list[int] = []

    def _flush(sec: int) -> None:
        if not bucket:
            return
        bucket.sort()
        out.append((sec, tuple(_percentile_ns(bucket, q) / 1e6 for q in qs)))
        bucket.clear()

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        lat = rec.get("latency")
        ts = rec.get("timestamp")
        if lat is None or ts is None:
            continue
        sec_epoch = _epoch_second(ts)
        if sec_epoch is None:
            continue
        if base_sec is None:
            base_sec = sec_epoch
        rel = sec_epoch - base_sec
        if cur_sec is None:
            cur_sec = rel
        if rel != cur_sec:
            _flush(cur_sec)
            cur_sec = rel
        bucket.append(int(lat))
    if cur_sec is not None:
        _flush(cur_sec)
    return out


def per_second_p99_from_ndjson(lines: "Iterable[str]") -> list[tuple[int, float]]:
    """Per-second p99 (ms) series: thin wrapper over the quantile bucketer."""
    return [
        (sec, vals[0])
        for sec, vals in per_second_quantiles_from_ndjson(lines, qs=(0.99,))
    ]


def _epoch_second(ts: str) -> int | None:
    """Integer epoch second from a vegeta RFC3339 timestamp (tolerant)."""
    from datetime import datetime

    s = ts.strip()
    # vegeta emits e.g. 2026-06-06T01:30:00.123456789Z; trim sub-second + Z and
    # parse to epoch seconds. We only need integer-second bucketing.
    try:
        head = s.replace("Z", "+00:00")
        # datetime.fromisoformat accepts <=6 fractional digits; clamp nanos.
        if "." in head:
            date_part, frac = head.split(".", 1)
            off = ""
            for marker in ("+", "-"):
                # offset sign after the fractional part
                pos = frac.find(marker)
                if pos > 0:
                    off = frac[pos:]
                    frac = frac[:pos]
                    break
            head = f"{date_part}.{frac[:6]}{off}"
        return int(datetime.fromisoformat(head).timestamp())
    except (ValueError, TypeError):
        return None


def write_per_second_p99_csv(series: list[tuple[int, float]], path: Path) -> None:
    """Write a `second,p99_ms` CSV (small artifact per probe)."""
    head = "second,p99_ms"
    rows = [f"{sec},{p99:.6f}" for sec, p99 in series]
    paths.write_text_atomic(path, "\n".join([head, *rows]) + "\n")


def encode_per_second_p99(
    *, exec_prefix: list[str], bin_in_container: str
) -> list[tuple[int, float]]:
    """Stream `vegeta encode --to json <bin>` and return per-second p99 (ms).

    `exec_prefix` is e.g. ["docker","exec","-T","ledgerline-vegeta-1"]. Uses a
    streaming subprocess so the NDJSON is consumed line-by-line (memory bounded);
    the .bin itself stays on the bench-io volume (only the small p99 series is
    pulled out).
    """
    return [
        (sec, vals[0])
        for sec, vals in encode_per_second_quantiles(
            exec_prefix=exec_prefix, bin_in_container=bin_in_container, qs=(0.99,)
        )
    ]


def encode_per_second_quantiles(
    *, exec_prefix: list[str], bin_in_container: str, qs: tuple[float, ...] = (0.99,)
) -> list[tuple[int, tuple[float, ...]]]:
    """Stream `vegeta encode --to json <bin>` into per-second quantiles (ms).

    Same streaming contract as `encode_per_second_p99`, generalized so the
    warmup flatness gates can pull (p99, p999) from the warmup .bin in one pass.
    """
    import subprocess

    argv = [*exec_prefix, "vegeta", "encode", "--to", "json", bin_in_container]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    try:
        series = per_second_quantiles_from_ndjson(iter(proc.stdout.readline, ""), qs=qs)
    finally:
        proc.stdout.close()
        proc.wait()
    return series
