"""Utilities: RFC3339 micros, atomic write, doctor spec-parse check."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ledgerbench import doctor
from ledgerbench.util import paths
from ledgerbench.util.clock import rfc3339_micros

RFC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def test_rfc3339_exactly_six_fractional_digits():
    s = rfc3339_micros(datetime(2026, 6, 6, 1, 2, 3, 4, tzinfo=timezone.utc))
    assert RFC.match(s), s
    assert s == "2026-06-06T01:02:03.000004Z"


def test_rfc3339_now_format():
    assert RFC.match(rfc3339_micros())


def test_write_text_atomic_no_leftover(tmp_path):
    p = tmp_path / "sub" / "x.json"
    paths.write_text_atomic(p, '{"a":1}')
    assert p.read_text() == '{"a":1}'
    assert list(tmp_path.glob("sub/.x.json.*")) == []


def test_repo_root_resolves():
    root = paths.repo_root()
    assert (root / "spec" / "workload.yaml").exists()
    assert (root / "bench").is_dir()


def test_doctor_spec_parses_ok():
    report = doctor.run_doctor(require_docker=False)
    by_name = {c.name: c for c in report.checks}
    assert by_name["spec_parses"].ok
