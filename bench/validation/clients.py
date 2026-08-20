"""HTTP client config for the equivalence suite, driven by env.

LEDGER_GO_URL / LEDGER_DOTNET_URL select the two service base URLs. When either
is unset the equivalence tests skip cleanly with a clear reason (the suite is
pure-importable and CI-green with no services running; it only exercises the
live services when the env points at them, i.e. during smoke/integration).

The client is a thin httpx wrapper that returns (status, headers, parsed-json)
and never raises on non-2xx (the suite asserts on status explicitly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

ENV_GO_URL = "LEDGER_GO_URL"
ENV_DOTNET_URL = "LEDGER_DOTNET_URL"

# Default per-request timeout for equivalence calls (generous; not a perf path).
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Service:
    """One service under test."""

    name: str        # "go" | "dotnet"
    base_url: str    # e.g. http://127.0.0.1:8081


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    json_body: object | None
    text: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


def configured_services() -> list[Service]:
    """Return the services whose base URL env var is set (possibly empty)."""
    out: list[Service] = []
    go = os.environ.get(ENV_GO_URL)
    dn = os.environ.get(ENV_DOTNET_URL)
    if go:
        out.append(Service(name="go", base_url=go.rstrip("/")))
    if dn:
        out.append(Service(name="dotnet", base_url=dn.rstrip("/")))
    return out


def both_services_configured() -> bool:
    return len(configured_services()) == 2


def skip_reason() -> str | None:
    """A clear skip message when the suite cannot run live, else None."""
    have = {s.name for s in configured_services()}
    missing = []
    if "go" not in have:
        missing.append(ENV_GO_URL)
    if "dotnet" not in have:
        missing.append(ENV_DOTNET_URL)
    if missing:
        return (
            f"equivalence suite needs both services live; unset: "
            f"{', '.join(missing)} (set them to the SUT base URLs to run)"
        )
    return None


def request(
    svc: Service,
    method: str,
    path: str,
    *,
    json_body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """Issue one request to a service; never raises on HTTP status."""
    url = f"{svc.base_url}{path}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, json=json_body, headers=headers)
    parsed: object | None
    try:
        parsed = resp.json()
    except Exception:
        parsed = None
    return Response(
        status=resp.status_code,
        headers={k.lower(): v for k, v in resp.headers.items()},
        json_body=parsed,
        text=resp.text,
    )
