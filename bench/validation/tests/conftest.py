"""Validation-suite fixtures.

The pure-logic tests (normalizer rules, golden self-consistency, shapecheck)
run with NO docker and NO services. The live equivalence tests skip cleanly
unless BOTH LEDGER_GO_URL and LEDGER_DOTNET_URL are set. (The docker-marked
harness tests this file once served: no-drop, sink microbench, CO, conn-sweep
were descoped at scope freeze and deleted; the `docker` marker and its autouse
skip fixture are kept for any future docker-needing test.)
"""

from __future__ import annotations

import pytest

from ledgerbench import compose
from validation import clients


@pytest.fixture(scope="session")
def services():
    """The two configured services, or skip if both are not live."""
    reason = clients.skip_reason()
    if reason:
        pytest.skip(reason)
    return clients.configured_services()


@pytest.fixture(autouse=True)
def _skip_docker_without_daemon(request):
    if request.node.get_closest_marker("docker") and not compose.docker_available():
        pytest.skip("docker daemon not available")
