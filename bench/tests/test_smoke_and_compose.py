"""Smoke flow plan + compose command builders (no docker daemon touched)."""

from __future__ import annotations

from ledgerbench import compose, smoke
from ledgerbench.compose import ComposeEnv


def test_smoke_dry_run_plan_shape():
    report = smoke.run_smoke(dry_run=True)
    details = [s.detail for s in report.steps]
    joined = "\n".join(details)
    # the smoke ordered flow appears
    assert "build" in joined
    assert "--scale smoke" in joined
    assert "--profile seed" in joined
    assert "30s vegeta probe" in joined or "30s" in joined
    assert "down" in joined and "-v" in joined
    assert report.ok  # dry-run plan is all-ok


def test_smoke_resource_accounting_off_non_linux():
    report = smoke.run_smoke(dry_run=True)
    assert "disabled" in report.resource_accounting or "off" in report.resource_accounting
    assert report.label == "smoke-only"


def test_compose_base_argv_profiles():
    env = ComposeEnv.base(profiles=[compose.PROFILE_GO, compose.PROFILE_LOAD])
    argv = env.base_argv()
    assert argv[:2] == ["docker", "compose"]
    assert "--profile" in argv and "go" in argv and "load" in argv
    assert "-p" in argv and "ledgerline" in argv


def test_compose_bench_overlay_adds_second_file():
    base = ComposeEnv.base()
    overlay = ComposeEnv.base(bench_overlay=True)
    assert len(base.files) == 1
    assert len(overlay.files) == 2
    assert overlay.files[1].name == "compose.bench.yaml"


def test_up_wait_and_down_builders():
    env = ComposeEnv.base(profiles=[compose.PROFILE_GO])
    up = compose.up_wait_argv(env, [compose.SVC_SUT_GO])
    assert "up" in up and "--wait" in up and "-d" in up
    down = compose.down_argv(env, volumes=True)
    assert down[-1] == "-v" and "down" in down


def test_exec_argv_no_tty():
    env = ComposeEnv.base()
    argv = compose.exec_argv(env, compose.SVC_VEGETA, ["vegeta", "--version"])
    assert "exec" in argv and "-T" in argv
    assert argv[-2:] == ["vegeta", "--version"]


def test_seed_oneshot_argv():
    argv = smoke.seed_argv(scale="smoke", seed=42)
    joined = " ".join(argv)
    assert "run" in argv and "--rm" in argv
    assert "--scale smoke" in joined and "--seed 42" in joined
