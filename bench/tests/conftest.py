"""Shared fixtures. Loads the real spec configs once (the single source of truth).

The pure-logic tests run with NO docker. Docker-needing tests are marked
@pytest.mark.docker and skipped when the daemon is absent.
"""

from __future__ import annotations

import pytest

from ledgerbench import compose, config


@pytest.fixture(scope="session")
def workload():
    return config.load_workload()


@pytest.fixture(scope="session")
def slo():
    return config.load_slo()


@pytest.fixture(autouse=True)
def _skip_docker_without_daemon(request):
    """Auto-skip @pytest.mark.docker tests when no daemon is reachable."""
    if request.node.get_closest_marker("docker") and not compose.docker_available():
        pytest.skip("docker daemon not available")
