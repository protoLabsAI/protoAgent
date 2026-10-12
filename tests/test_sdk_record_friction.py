"""``graph.sdk.record_friction`` — a plugin's background work records friction in the ledger.

The agent's ``record_friction`` tool only sees what goes wrong inside a turn. protoEngineer's
friction log stayed empty for two weeks of heavy projectBoard trouble (hung coder drives, a
missing epic base, preflight holds), because all of it happened in the board's background
loop. The seam writes through the friction plugin's ``friction.record`` service (ADR 0116):
the same ledger and grouping as the tool, and a no-op when the plugin is off.
"""

from __future__ import annotations

import json

import pytest

from graph import plugin_services, sdk
from graph.plugins.testkit import FakeRegistry
from plugins import friction


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(p))
    return p


@pytest.fixture
def friction_on():
    """Wire the friction plugin's services the way the loader does."""
    reg = FakeRegistry(plugin_id="friction")
    friction.register(reg)
    plugin_services.set_plugin_services(reg.services)
    return reg


def _recs(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_friction_plugin_offers_the_record_service(friction_on):
    assert friction_on.services["friction.record"] is friction.record_service


def test_a_plugin_records_into_the_same_ledger_the_review_reads(ledger, friction_on):
    rid = sdk.record_friction(
        "harness",
        "coder drive hung (no output before the timeout)",
        detail="card bd-1, 900s",
        severity="major",
        source="project_board",
    )
    assert rid and len(rid) == 8
    (rec,) = _recs(ledger)
    assert rec["kind"] == "harness" and rec["severity"] == "major"
    assert rec["source"] == "plugin:project_board" and rec["detail"] == "card bd-1, 900s"

    # A stable summary groups: the second occurrence is the SAME row, with a count.
    rid2 = sdk.record_friction(
        "harness", "coder drive hung (no output before the timeout)", detail="card bd-2", source="project_board"
    )
    assert rid2 == rid
    (group,) = friction.grouped_entries()
    assert group["id"] == rid and group["count"] == 2 and group["severity"] == "major"


def test_it_is_a_no_op_when_the_friction_plugin_is_off(ledger):
    plugin_services.set_plugin_services({})
    assert sdk.record_friction("harness", "anything", source="project_board") is None
    assert not ledger.exists()


@pytest.mark.parametrize(("kind", "summary"), [("bogus", "x"), ("harness", "   "), ("", "x")])
def test_invalid_input_is_refused_without_raising(ledger, friction_on, kind, summary):
    assert sdk.record_friction(kind, summary, source="p") == ""
    assert not ledger.exists()


def test_an_unknown_severity_records_as_minor_and_the_source_is_sanitised(ledger, friction_on):
    sdk.record_friction("model", "s", severity="catastrophic", source="my plugin/../x")
    (rec,) = _recs(ledger)
    assert rec["severity"] == "minor" and rec["source"] == "plugin:my-plugin-..-x"


def test_a_provider_that_raises_never_breaks_the_caller(ledger):
    def _boom(*_a, **_k):
        raise RuntimeError("disk full")

    plugin_services.set_plugin_services({"friction.record": _boom})
    assert sdk.record_friction("harness", "s", source="p") == ""


def test_a_ledger_write_failure_never_breaks_the_caller(friction_on, monkeypatch):
    def _fail(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(friction, "_log", _fail)
    assert sdk.record_friction("harness", "s", source="p") == ""
