"""The CLI surface must DISCLOSE the operator escape hatches.

The runner now aborts by default on a non-passing capacity gate, and the only
two ways to change what it does are environment variables. If `--help` does not
name them, the operator's only source is the source code.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ledgerbench import phases
from ledgerbench.__main__ import app

runner = CliRunner()


def _help(*argv: str) -> str:
    res = runner.invoke(app, [*argv, "--help"])
    assert res.exit_code == 0, res.output
    # rich/click wraps help text at the terminal width; join it back up so the
    # assertions are about content, not line breaks.
    return " ".join(res.output.split())


def test_root_help_names_both_operator_env_vars():
    out = _help()
    assert phases.ALLOW_DB_IMPOSED_KNEE_ENV in out
    assert phases.CACHE_TTL_OVERRIDE_ENV in out


@pytest.mark.parametrize("cmd", ["run-all", "gate", "ladder", "confirm", "soak"])
def test_every_gate_touching_command_documents_the_opt_out(cmd):
    out = _help(cmd)
    assert phases.ALLOW_DB_IMPOSED_KNEE_ENV in out
    assert phases.CACHE_TTL_OVERRIDE_ENV in out


def test_run_all_help_states_the_abort_by_default_behaviour():
    out = _help("run-all")
    assert "ABORTS at the capacity gate" in out


@pytest.mark.parametrize("cmd", ["ladder", "confirm", "soak"])
def test_per_stage_help_says_it_re_asserts_the_gate(cmd):
    out = _help(cmd)
    assert "gate" in out.lower()
