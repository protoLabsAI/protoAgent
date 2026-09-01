"""Tests for the friction plugin — record/review + auto-capture middleware."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from plugins.friction import (
    FrictionMiddleware,
    friction_review,
    grouped_entries,
    record_friction,
    resolve_friction,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(p))
    return p


class _Req:
    """Minimal stand-in for the middleware's tool-call request."""

    def __init__(self, name, args=None):
        self.tool_call = {"name": name, "args": args or {}}


def _recs(ledger):
    return [json.loads(line) for line in ledger.read_text().splitlines()]


# ── record_friction / friction_review ────────────────────────────────────────


def test_record_and_review(ledger):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "no disk tool", "severity": "major"}))
    asyncio.run(record_friction.ainvoke({"kind": "model", "summary": "took a wrong path"}))
    out = asyncio.run(friction_review.ainvoke({}))
    assert "harness=1" in out and "model=1" in out
    assert "took a wrong path" in asyncio.run(friction_review.ainvoke({"kind": "model"}))
    assert "no disk tool" not in asyncio.run(friction_review.ainvoke({"kind": "model"}))


def test_record_validates(ledger):
    assert "kind must be" in asyncio.run(record_friction.ainvoke({"kind": "bogus", "summary": "x"}))
    assert "summary is required" in asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "  "}))


def test_review_empty(ledger):
    assert "empty" in asyncio.run(friction_review.ainvoke({}))


# ── FrictionMiddleware auto-capture ──────────────────────────────────────────


def test_middleware_logs_real_error_and_reraises(ledger):
    mw = FrictionMiddleware()

    def boom(_req):
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        mw.wrap_tool_call(_Req("some_tool"), boom)
    assert any(r["source"] == "auto" and "raised" in r["summary"] for r in _recs(ledger))


def test_middleware_filters_control_flow(ledger):
    mw = FrictionMiddleware()
    interrupt = type("GraphInterrupt", (Exception,), {})  # name is in _CONTROL_FLOW

    def pause(_req):
        raise interrupt("approval needed")

    with pytest.raises(interrupt):
        mw.wrap_tool_call(_Req("run_command", {"command": "df -h"}), pause)
    recs = _recs(ledger)
    # the escape-hatch reach IS logged; the HITL interrupt is NOT logged as an error
    assert any("escape hatch" in r["summary"] for r in recs)
    assert not any("raised" in r["summary"] for r in recs)


def test_middleware_notes_escape_hatch(ledger):
    mw = FrictionMiddleware()

    def ok(_req):
        return "fine"

    assert mw.wrap_tool_call(_Req("run_command", {"command": "ls"}), ok) == "fine"
    recs = _recs(ledger)
    assert any(r.get("tool") == "run_command" and "escape hatch" in r["summary"] for r in recs)


def test_middleware_ignores_normal_tools(ledger):
    mw = FrictionMiddleware()

    def ok(_req):
        return "ok"

    assert mw.wrap_tool_call(_Req("gpu_status"), ok) == "ok"
    assert not ledger.exists() or _recs(ledger) == []


# ── #2595: the ledger was write-only — a read path, dedup, and a size bound ──


def _seed(monkeypatch, tmp_path, records):
    from plugins import friction

    log = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(log))
    for kind, summary, severity in records:
        friction._log(kind, summary, "detail", severity, source="auto")
    return log


def test_repeats_collapse_into_one_item_with_a_count(monkeypatch, tmp_path):
    """The escape-hatch signal was recorded five times and read as five rows of noise. It
    is ONE thing that kept happening, which is the only framing a triage list can act on."""
    from plugins import friction

    _seed(
        monkeypatch,
        tmp_path,
        [("harness", "reached for escape hatch 'shell'", "minor")] * 5
        + [("harness", "tool 'board_list' raised", "major")],
    )

    groups = friction.grouped_entries()

    by_summary = {g["summary"]: g for g in groups}
    assert by_summary["reached for escape hatch 'shell'"]["count"] == 5
    assert by_summary["tool 'board_list' raised"]["count"] == 1
    assert len(groups) == 2


def test_a_group_keeps_the_worst_severity_seen(monkeypatch, tmp_path):
    """One major occurrence makes the group worth looking at, however many minor ones
    surround it — otherwise a burst of minors would bury it."""
    from plugins import friction

    _seed(
        monkeypatch,
        tmp_path,
        [("harness", "same thing", "minor"), ("harness", "same thing", "major"), ("harness", "same thing", "minor")],
    )

    assert friction.grouped_entries()[0]["severity"] == "major"


def test_identical_timestamps_still_order_newest_written_first(monkeypatch, tmp_path):
    """A burst shares one timestamp on a coarse clock (Windows), and a stable sort then
    hands back READ order — oldest first, the inverse of the promise (#2616). Ties must
    break on ledger position, so this pins the invariant on every platform instead of
    leaving it to clock resolution."""
    from plugins import friction

    log = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(log))
    frozen = "2026-08-11T12:00:00+00:00"
    log.write_text(
        "\n".join(
            json.dumps({"ts": frozen, "kind": "harness", "summary": s, "detail": "d", "severity": "minor"})
            for s in ("older", "newer")
        )
        + "\n",
        encoding="utf-8",
    )

    assert [g["summary"] for g in friction.grouped_entries()] == ["newer", "older"]


def test_groups_are_newest_first_and_carry_a_window(monkeypatch, tmp_path):
    from plugins import friction

    _seed(monkeypatch, tmp_path, [("harness", "older", "minor"), ("model", "newer", "minor")])

    groups = friction.grouped_entries()
    assert [g["summary"] for g in groups] == ["newer", "older"]
    assert groups[0]["first_seen"] and groups[0]["last_seen"]


def test_kind_filter(monkeypatch, tmp_path):
    from plugins import friction

    _seed(monkeypatch, tmp_path, [("harness", "h", "minor"), ("model", "m", "minor")])

    assert [g["summary"] for g in friction.grouped_entries("model")] == ["m"]


def test_the_ledger_is_bounded(monkeypatch, tmp_path):
    """It grew forever. An agent hitting the same friction all day must not be able to
    grow this file without limit."""
    from plugins import friction

    monkeypatch.setattr(friction, "_MAX_ENTRIES", 10)
    monkeypatch.setattr(friction, "_TRIM_SLACK", 2)
    log = _seed(monkeypatch, tmp_path, [("harness", f"entry {i}", "minor") for i in range(40)])

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) <= 12
    assert "entry 39" in lines[-1]  # the NEWEST survive, not the oldest


def test_non_ascii_friction_round_trips(monkeypatch, tmp_path):
    """A friction summary quoting an error with an em dash or a non-ASCII path was written
    with the locale code page on Windows and read back as mojibake (the #2521 class)."""
    from plugins import friction

    _seed(monkeypatch, tmp_path, [("harness", "path /Users/café — failed", "minor")])

    assert friction.grouped_entries()[0]["summary"] == "path /Users/café — failed"


def test_read_api_returns_grouped_items(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins import friction

    _seed(monkeypatch, tmp_path, [("harness", "repeated", "minor")] * 3 + [("model", "once", "minor")])
    app = FastAPI()
    app.include_router(friction._build_router(), prefix="/api/plugins/friction")
    app.include_router(friction._build_router(legacy=True))  # as register() mounts them
    client = TestClient(app)

    body = client.get("/api/friction").json()

    assert body["total"] == 2 and body["grouped"] is True
    assert {i["summary"]: i["count"] for i in body["items"]} == {"repeated": 3, "once": 1}
    assert body["counts"] == {"harness": 3, "model": 1}

    raw = client.get("/api/friction", params={"grouped": False}).json()
    assert raw["total"] == 4  # ungrouped returns every record
    assert client.get("/api/friction", params={"kind": "bogus"}).json()["items"] == []


# ── #2990: resolve_friction — dismiss resolved entries from the backlog ──────


def _record(kind, summary):
    asyncio.run(record_friction.ainvoke({"kind": kind, "summary": summary}))


def test_resolve_marks_all_matching_entries_in_place(ledger):
    """Substring match stamps EVERY matching record with resolved_at; nothing is deleted —
    the ledger is append-only and the audit trail must survive a resolve."""
    from datetime import datetime

    _record("harness", "board_cancel_feature is missing")
    _record("harness", "board_cancel_feature raised")
    _record("model", "took a wrong path")

    out = asyncio.run(resolve_friction.ainvoke({"summary": "board_cancel_feature", "reason": "tool shipped in #2984"}))

    assert "resolved 2" in out
    recs = _recs(ledger)
    assert len(recs) == 3  # in-place stamp, not a delete
    resolved = [r for r in recs if r.get("resolved_at")]
    assert {r["summary"] for r in resolved} == {"board_cancel_feature is missing", "board_cancel_feature raised"}
    for r in resolved:
        datetime.fromisoformat(r["resolved_at"])  # a real ISO timestamp
        assert r["resolved_reason"] == "tool shipped in #2984"
    assert not next(r for r in recs if r["summary"] == "took a wrong path").get("resolved_at")


def test_resolve_with_no_match_reports_it(ledger):
    _record("harness", "something real")
    assert "no matching entries found" in asyncio.run(resolve_friction.ainvoke({"summary": "never recorded"}))
    assert not _recs(ledger)[0].get("resolved_at")  # and it touched nothing


def test_resolve_on_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    assert "no matching entries found" in asyncio.run(resolve_friction.ainvoke({"summary": "anything"}))


def test_review_excludes_resolved_by_default(ledger):
    _record("harness", "fixed thing")
    _record("harness", "still broken")
    asyncio.run(resolve_friction.ainvoke({"summary": "fixed thing"}))

    out = asyncio.run(friction_review.ainvoke({}))

    assert "still broken" in out and "fixed thing" not in out
    assert "harness=1" in out  # counts reflect the live backlog, not history


def test_review_includes_resolved_with_marker(ledger):
    _record("harness", "fixed thing")
    _record("harness", "still broken")
    asyncio.run(resolve_friction.ainvoke({"summary": "fixed thing"}))

    out = asyncio.run(friction_review.ainvoke({"include_resolved": True}))

    resolved_line = next(ln for ln in out.splitlines() if "fixed thing" in ln)
    live_line = next(ln for ln in out.splitlines() if "still broken" in ln)
    assert "[resolved]" in resolved_line
    assert "[resolved]" not in live_line


def test_review_all_resolved_reads_as_empty_backlog(ledger):
    _record("harness", "fixed thing")
    asyncio.run(resolve_friction.ainvoke({"summary": "fixed thing"}))
    assert "no  friction recorded" in asyncio.run(friction_review.ainvoke({}))


def test_grouped_entries_respect_the_resolved_filter(ledger):
    from plugins import friction

    _record("harness", "fixed thing")
    _record("harness", "fixed thing")
    asyncio.run(resolve_friction.ainvoke({"summary": "fixed thing"}))

    assert friction.grouped_entries() == []
    groups = friction.grouped_entries(include_resolved=True)
    assert groups[0]["count"] == 2 and groups[0]["resolved_at"]


def test_read_api_filters_resolved(ledger):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins import friction

    _record("harness", "fixed thing")
    _record("harness", "still broken")
    asyncio.run(resolve_friction.ainvoke({"summary": "fixed thing"}))
    app = FastAPI()
    app.include_router(friction._build_router(), prefix="/api/plugins/friction")
    app.include_router(friction._build_router(legacy=True))  # as register() mounts them
    client = TestClient(app)

    body = client.get("/api/friction").json()
    assert {i["summary"] for i in body["items"]} == {"still broken"}
    assert body["counts"] == {"harness": 1, "model": 0}

    both = client.get("/api/friction", params={"resolved": True}).json()
    assert {i["summary"] for i in both["items"]} == {"fixed thing", "still broken"}

    raw = client.get("/api/friction", params={"resolved": True, "grouped": False}).json()
    assert next(i for i in raw["items"] if i["summary"] == "fixed thing")["resolved_at"]
    assert not next(i for i in raw["items"] if i["summary"] == "still broken").get("resolved_at")


# ── the console's triage write path — POST /api/friction/resolve ─────────────


def _client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins import friction

    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    app = FastAPI()
    app.include_router(friction._build_router(), prefix="/api/plugins/friction")
    app.include_router(friction._build_router(legacy=True))  # as register() mounts them
    return TestClient(app)


def test_console_resolve_stamps_the_ledger_the_agent_reads(monkeypatch, tmp_path):
    """The point of the endpoint: one backlog. A row resolved in the console must drop
    out of the agent's own friction_review, not just out of one operator's browser."""
    client = _client(monkeypatch, tmp_path)
    _record("harness", "board priority is immutable")
    _record("harness", "still broken")

    body = client.post(
        "/api/friction/resolve",
        json={"summary": "board priority is immutable", "kind": "harness"},
    ).json()

    assert body == {
        "changed": 1,
        "resolved": True,
        "summary": "board priority is immutable",
        "kind": "harness",
    }
    review = asyncio.run(friction_review.ainvoke({}))
    assert "board priority is immutable" not in review
    assert "still broken" in review
    assert [i["summary"] for i in client.get("/api/friction").json()["items"]] == ["still broken"]


def test_console_resolve_matches_the_row_exactly_not_a_substring(monkeypatch, tmp_path):
    """The tool matches a substring on purpose; the console must NOT. It acts on one
    grouped row, and a substring match would silently resolve every other row whose
    summary happens to contain this one's text."""
    client = _client(monkeypatch, tmp_path)
    _record("harness", "read_file truncates")
    _record("harness", "read_file truncates at 50k chars on a 167KB file")

    assert client.post("/api/friction/resolve", json={"summary": "read_file truncates"}).json()["changed"] == 1

    still_open = [i["summary"] for i in client.get("/api/friction").json()["items"]]
    assert still_open == ["read_file truncates at 50k chars on a 167KB file"]


def test_console_resolve_is_scoped_to_the_rows_kind(monkeypatch, tmp_path):
    """Groups are keyed on (kind, summary) — the same summary in both channels is two
    rows, and resolving one must not take the other with it."""
    client = _client(monkeypatch, tmp_path)
    _record("harness", "same words")
    _record("model", "same words")

    assert client.post("/api/friction/resolve", json={"summary": "same words", "kind": "model"}).json()["changed"] == 1

    open_rows = {(i["kind"], i["summary"]) for i in client.get("/api/friction").json()["items"]}
    assert open_rows == {("harness", "same words")}


def test_console_can_reopen_a_resolved_row(monkeypatch, tmp_path):
    """Reopening clears the stamp, so a row resolved by mistake comes back — and the
    entry itself is still there, never deleted."""
    client = _client(monkeypatch, tmp_path)
    _record("harness", "premature")
    client.post("/api/friction/resolve", json={"summary": "premature", "reason": "thought it was fixed"})

    body = client.post("/api/friction/resolve", json={"summary": "premature", "resolved": False}).json()

    assert body["changed"] == 1 and body["resolved"] is False
    assert [i["summary"] for i in client.get("/api/friction").json()["items"]] == ["premature"]
    rec = _recs(tmp_path / "friction.jsonl")[0]
    assert "resolved_at" not in rec and "resolved_reason" not in rec


def test_console_resolve_is_idempotent_and_reports_no_change(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _record("harness", "once")
    assert client.post("/api/friction/resolve", json={"summary": "once"}).json()["changed"] == 1
    assert client.post("/api/friction/resolve", json={"summary": "once"}).json()["changed"] == 0


def test_console_resolve_rejects_an_empty_summary_and_a_bogus_kind(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _record("harness", "untouched")

    assert client.post("/api/friction/resolve", json={"summary": "  "}).json()["changed"] == 0
    assert "summary is required" in client.post("/api/friction/resolve", json={}).json()["error"]
    assert "kind must be one of" in client.post(
        "/api/friction/resolve", json={"summary": "untouched", "kind": "bogus"}
    ).json()["error"]
    assert not _recs(tmp_path / "friction.jsonl")[0].get("resolved_at")


def test_a_resolve_never_truncates_the_ledger(monkeypatch, tmp_path):
    """The rewrite is atomic (temp + os.replace). The audit trail is the reason resolve
    stamps in place instead of deleting, so the write that does it must not be able to
    lose it — and it must leave no .tmp litter behind."""
    client = _client(monkeypatch, tmp_path)
    for i in range(20):
        _record("harness", f"entry {i}")

    client.post("/api/friction/resolve", json={"summary": "entry 7"})

    ledger = tmp_path / "friction.jsonl"
    assert len(_recs(ledger)) == 20  # every record survived
    assert not list(tmp_path.glob("*.tmp"))


def test_auto_capture_detail_is_json_the_view_can_render(ledger):
    """The captured args are stored as JSON, not a Python dict repr — the console shows
    this string to an operator, and `{'command': 'git diff'}` parses as nothing."""
    mw = FrictionMiddleware()
    mw._note_escape_hatch(_Req("shell", {"command": "git diff", "cwd": "/repo"}))

    detail = _recs(ledger)[0]["detail"]
    assert json.loads(detail) == {"command": "git diff", "cwd": "/repo"}


# ── ADR 0079 seam: the backlog reaches the agent's working state ─────────────


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The plugin with a registry attached, as the host wires it at boot.

    ``FakeRegistry`` is the shipped testkit, not a hand-rolled stub, on purpose: it mirrors
    the real ``PluginRegistry`` surface under a parity test, and it RAISES where the live
    registry warns-and-skips — so a registration the host would silently drop fails here
    instead of shipping green."""
    from graph.plugins.testkit import FakeRegistry

    from plugins import friction

    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    friction._WORK_CACHE["stamp"] = None  # module-level cache, per-test

    reg = FakeRegistry(plugin_id="friction", plugin_dir=Path(friction.__file__).parent)
    friction.register(reg)
    monkeypatch.setattr(friction, "_REGISTRY", reg)
    yield reg
    friction._REGISTRY = None


def test_a_quiet_ledger_contributes_nothing_to_working_state(wired):
    """The property that makes this safe on by default: `<working_state>` is a shared,
    bounded budget, so an instance with no real friction must add no lines at all."""
    from plugins.friction import open_friction_work

    assert open_friction_work() == []
    _record("harness", "a one-off minor annoyance")
    assert open_friction_work() == []  # minor, seen once — not worth a turn's attention


def test_major_and_repeated_friction_reach_the_working_state(wired):
    from plugins.friction import open_friction_work

    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "blocked hard", "severity": "major"}))
    for _ in range(3):
        _record("harness", "keeps happening")
    _record("harness", "seen once")

    items = open_friction_work()

    assert [i["title"] for i in items] == ["blocked hard", "keeps happening"]
    assert items[0]["state"] == "major"
    assert items[1]["state"] == "minor x3"  # the count IS the argument for looking
    assert "seen once" not in [i["title"] for i in items]


def test_working_state_is_bounded_and_configurable(wired):
    from plugins.friction import open_friction_work

    for i in range(10):
        asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": f"major {i}", "severity": "major"}))

    assert len(open_friction_work()) == 3  # default cap

    wired.config = {"working_state_limit": 5}
    assert len(open_friction_work()) == 5
    wired.config = {"working_state": False}
    assert open_friction_work() == []  # an operator can turn the injection off entirely


def test_resolved_friction_leaves_the_working_state(wired):
    from plugins.friction import open_friction_work

    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "fix me", "severity": "major"}))
    assert [i["title"] for i in open_friction_work()] == ["fix me"]

    asyncio.run(resolve_friction.ainvoke({"summary": "fix me"}))

    assert open_friction_work() == []


def test_the_work_snapshot_does_not_reparse_an_unchanged_ledger(wired, monkeypatch):
    """It runs inline on EVERY turn, so an unchanged ledger must cost a stat, not a parse."""
    from plugins import friction

    _record("harness", "something")
    friction.open_friction_work()

    calls = []
    monkeypatch.setattr(friction, "grouped_entries", lambda *a, **k: calls.append(1) or [])
    friction.open_friction_work()
    friction.open_friction_work()
    assert calls == []  # cache hit — the file never changed

    _record("harness", "a new one")  # mutates the ledger → cache must invalidate
    friction.open_friction_work()
    assert len(calls) == 1


def test_the_plugin_registers_its_seams(wired):
    """A core plugin should reach the agent through more than a tool list."""
    assert "backlog" in wired.work_providers
    assert wired.work_provider_meta["backlog"]["label"] == "OPEN FRICTION"
    assert wired.skill_dirs == ["skills"]


# ── ADR 0039 seam: friction is broadcast, so other plugins can act on it ─────


def test_recording_and_resolving_emit_on_the_bus(wired):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "worth hearing", "severity": "major"}))

    topic, data = wired.emitted[-1]
    assert topic == "friction.recorded"  # auto-namespaced by the bus
    assert data["summary"] == "worth hearing" and data["severity"] == "major" and data["kind"] == "harness"

    asyncio.run(resolve_friction.ainvoke({"summary": "worth hearing", "reason": "shipped"}))
    assert wired.emitted[-1][0] == "friction.resolved"
    assert wired.emitted[-1][1]["count"] == 1 and wired.emitted[-1][1]["reason"] == "shipped"


def test_a_bus_failure_never_breaks_a_tool_call(wired):
    """Friction recording is the system's self-improvement path; it must not be the thing
    that fails a turn."""
    def _boom(topic, data=None):
        raise RuntimeError("bus down")

    monkeypatch_target = wired
    monkeypatch_target.emit = _boom
    out = asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "still logged"}))

    assert "logged" in out
    assert _recs(_ledger_for(wired))[0]["summary"] == "still logged"


def _ledger_for(_registry):
    from plugins.friction import _ledger_path

    return _ledger_path()


# ── the operator's control command — user-only by design ────────────────────


def _slash(wired, rest=""):
    return asyncio.run(wired.chat_commands["friction"](rest, "session-1"))


def test_friction_slash_summarises_worst_first(wired):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "blocks everything", "severity": "major"}))
    for _ in range(2):
        _record("harness", "nags twice")

    out = _slash(wired)

    assert "3 occurrences" in out and "2 open signals" in out and "1 major" in out
    # worst-first, the same ranking the working-state projection uses
    assert out.index("blocks everything") < out.index("nags twice")
    assert "×2" in out


def test_friction_slash_resolves_and_says_how_many(wired):
    _record("harness", "board priority is immutable")
    _record("harness", "board priority is immutable")
    _record("harness", "unrelated")

    assert "Resolved **2** entries" in _slash(wired, "board priority")
    assert [g["summary"] for g in grouped_entries()] == ["unrelated"]


def test_friction_slash_reports_a_miss_instead_of_claiming_success(wired):
    _record("harness", "real")
    out = _slash(wired, "never recorded")
    assert "No open friction matching" in out
    assert not [g for g in grouped_entries() if g.get("resolved_at")]


def test_friction_slash_on_an_empty_backlog(wired):
    assert "backlog is empty" in _slash(wired)


def test_resolving_is_operator_only_never_an_agent_tool(wired):
    """`/friction <text>` resolves, and it is registered as a chat command rather than a
    tool ON PURPOSE: the model must not be able to clear its own backlog by deciding it is
    clear. The agent's `resolve_friction` tool still exists for a rough edge it actually
    fixed — that is a different claim."""
    assert "friction" in wired.chat_commands
    assert "friction" not in {getattr(t, "name", "") for t in wired.tools}


# ── delegation + federation seams ───────────────────────────────────────────


def test_the_triage_subagent_is_read_only(wired):
    """Triage decides what to FILE. It must not be able to resolve what it just decided
    to file — that claim belongs to the operator, or to a fix that actually landed."""
    sub = next(s for s in wired.subagents if s.name == "friction_triage")

    assert sub.tools == ["friction_review"]
    assert "resolve_friction" not in sub.tools and "record_friction" not in sub.tools
    assert sub.default_prompt  # dispatchable bare, via /task with no prompt


def test_the_agent_card_advertises_friction_review(wired):
    """A peer should be able to ask this agent what has been getting in its way."""
    skill = next(s for s in wired.a2a_skills if s["id"] == "friction-review")

    assert skill["name"] and skill["description"]
    assert "diagnostics" in skill["tags"]


def test_every_seam_this_plugin_claims_is_actually_registered(wired):
    """The point of the 0.2 rebuild: friction reaches the agent through the seams the
    plugin system provides, not through a tool list alone. If one is dropped, say so
    here rather than discovering it as a silently missing surface."""
    assert {getattr(t, "name", "") for t in wired.tools} == {
        "record_friction", "friction_review", "resolve_friction",
    }
    assert wired.middlewares                      # auto-capture
    assert wired.routers                          # read API + resolve + the view page
    assert wired.work_providers                   # ADR 0079 working state
    assert wired.skill_dirs == ["skills"]         # when to record
    assert "friction" in wired.chat_commands      # operator control
    assert wired.subagents                        # triage delegate
    assert wired.a2a_skills                       # agent card


def test_distinct_errors_from_one_tool_are_distinct_signals(ledger):
    """Groups are keyed on (kind, summary), so a bare "tool 'task' raised" collapsed every
    failure of that tool into one row — a count spanning unrelated bugs argues for a fix
    nobody can scope."""
    mw = FrictionMiddleware()
    for exc in (RuntimeError("bad provider"), TimeoutError("gateway timeout"), RuntimeError("bad provider again")):
        mw._note_error(_Req("task"), exc)

    groups = {g["summary"]: g["count"] for g in grouped_entries()}

    assert groups == {"tool 'task' raised RuntimeError": 2, "tool 'task' raised TimeoutError": 1}


def test_an_over_long_report_is_marked_as_clipped_not_cut_mid_word(ledger):
    asyncio.run(record_friction.ainvoke({
        "kind": "harness", "summary": "s" * 400, "detail": "d" * 900,
    }))

    rec = _recs(ledger)[0]

    assert len(rec["summary"]) == 200 and rec["summary"].endswith("…")
    assert len(rec["detail"]) == 600 and rec["detail"].endswith("…")


def test_a_report_within_the_cap_is_untouched(ledger):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "short and complete."}))
    assert _recs(ledger)[0]["summary"] == "short and complete."


# ── ADR 0004: the ledger is instance-scoped ─────────────────────────────────


def test_the_ledger_lives_inside_the_instance_tree(tmp_path, monkeypatch):
    """It used to be derived from PROTOAGENT_HOME directly, which is only correct on the
    desktop (where that env var IS the instance root). Everywhere else the ledger landed
    one level ABOVE the instance tree — shared by every instance on the box and missed by
    scripts/dev-reset.sh. On by default makes that collision everyone's problem."""
    from infra.paths import instance_paths

    from plugins.friction import _ledger_path

    monkeypatch.delenv("FRICTION_LOG", raising=False)
    expected = Path(instance_paths().store("friction")) / "friction.jsonl"

    assert _ledger_path() == expected
    assert Path(instance_paths().explain()["instance_root"]) in expected.parents


def test_an_existing_pre_instance_ledger_is_adopted_not_orphaned(tmp_path, monkeypatch):
    """Upgrading must not look like losing your history."""
    from plugins import friction

    monkeypatch.delenv("FRICTION_LOG", raising=False)
    legacy = tmp_path / "legacy" / "friction" / "friction.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"ts":"2026-07-22T00:00:00+00:00","kind":"harness","summary":"old signal"}\n',
                      encoding="utf-8")
    new_home = tmp_path / "instance"
    monkeypatch.setattr(friction, "_legacy_ledger_path", lambda: legacy)
    monkeypatch.setattr("infra.paths.instance_paths", lambda: _FakePaths(new_home))

    resolved = friction._ledger_path()

    assert resolved == new_home / "friction" / "friction.jsonl"
    assert resolved.is_file() and not legacy.exists()   # moved, not copied or dropped
    assert "old signal" in resolved.read_text(encoding="utf-8")


class _FakePaths:
    def __init__(self, root):
        self._root = Path(root)

    def store(self, name):
        return self._root / name


# ── review follow-ups ───────────────────────────────────────────────────────


def test_bulk_resolve_names_what_it_resolved(wired):
    """`/friction <text>` matches a SUBSTRING, like the tool — an operator clearing
    "board_cancel" means every phrasing of it. A bulk action whose blast radius is
    invisible is how you resolve six signals meaning to resolve one, so it has to say."""
    _record("harness", "board_cancel refuses with open deps")
    _record("harness", "board_cancel cannot cancel bd-uwj")
    _record("harness", "something else entirely")

    out = asyncio.run(wired.chat_commands["friction"]("board_cancel", "s"))

    assert "board_cancel refuses with open deps" in out
    assert "board_cancel cannot cancel bd-uwj" in out
    assert "something else entirely" not in out
    assert "Reopen" in out  # and it says the action is recoverable
    assert [g["summary"] for g in grouped_entries()] == ["something else entirely"]


def test_the_operator_and_the_agent_rank_the_backlog_identically(wired):
    """Three copies of this ordering existed, one of them mislabelled as matching the
    others. If they disagree, the operator and the agent read different lists off the
    same ledger."""
    from plugins.friction import _triage_rank, open_friction_work

    for n in range(4):
        asyncio.run(record_friction.ainvoke(
            {"kind": "harness", "summary": f"repeated {n}", "severity": "minor"}))
    for _ in range(5):
        _record("harness", "repeated 0")
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "one major", "severity": "major"}))

    wired.config = {"working_state_limit": 10, "working_state_repeat_threshold": 1}
    agent_order = [i["title"] for i in open_friction_work()]
    operator_order = [g["summary"] for g in sorted(grouped_entries(), key=_triage_rank, reverse=True)]

    assert agent_order == operator_order[: len(agent_order)]
    assert agent_order[0] == "one major"


def test_auto_captured_payloads_are_clipped_with_a_marker_too(ledger):
    """The clip marker was added to `record_friction` but the auto-capture path kept a
    bare 300-char slice, so the payload an operator actually reads still stopped
    mid-word — and the view's "capped on write" notice never fired for it."""
    mw = FrictionMiddleware()
    mw._note_escape_hatch(_Req("shell", {"command": "x" * 500}))

    detail = _recs(ledger)[0]["detail"]
    assert len(detail) == 300 and detail.endswith("…")


def test_the_working_state_provider_stays_far_inside_its_inline_budget(wired):
    """It runs inline on EVERY turn. `graph.work_providers.SLOW_PROVIDER_S` is the line;
    measured ~6.5ms cold at the ledger's 2000-entry cap, so this asserts an order of
    magnitude of headroom rather than the exact number."""
    import time

    from graph.work_providers import SLOW_PROVIDER_S
    from plugins.friction import _WORK_CACHE, open_friction_work

    path = _ledger_for(wired)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(2000):
            fh.write(json.dumps({
                "ts": f"2026-08-{(i % 28) + 1:02d}T00:00:00+00:00", "kind": "harness",
                "summary": f"signal {i % 97}", "severity": "major", "source": "auto",
                "detail": "d" * 400,
            }) + "\n")

    _WORK_CACHE["stamp"] = None
    started = time.perf_counter()
    open_friction_work()
    cold = time.perf_counter() - started

    assert cold < SLOW_PROVIDER_S / 5, f"cold projection took {cold * 1000:.0f}ms"


def test_the_api_serves_its_own_namespace_and_keeps_the_documented_alias(monkeypatch, tmp_path):
    """Plugin-view Rule 2 wants the data API under /api/plugins/<id>/. #2607 shipped it at
    the top-level /api/friction and that path is documented and in use, so the namespaced
    mount is ADDED and the old one kept — rather than breaking a published API to satisfy
    a convention."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins import friction

    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    app = FastAPI()
    app.include_router(friction._build_router(), prefix="/api/plugins/friction")
    app.include_router(friction._build_router(legacy=True))
    client = TestClient(app)
    _record("harness", "reachable both ways")

    canonical = client.get("/api/plugins/friction/").json()
    legacy = client.get("/api/friction").json()

    assert canonical == legacy
    assert [i["summary"] for i in canonical["items"]] == ["reachable both ways"]
    assert client.post("/api/plugins/friction/resolve", json={"summary": "reachable both ways"}).json()["changed"] == 1
    assert client.post("/api/friction/resolve", json={"summary": "reachable both ways", "resolved": False}).json()["changed"] == 1


def test_the_view_calls_the_namespaced_api(monkeypatch):
    """The page is the one caller we control; it should use the canonical path so the
    alias only ever serves external callers."""
    from pathlib import Path as _P

    from plugins import friction

    page = (_P(friction.__file__).parent / "view.html").read_text(encoding="utf-8")

    assert 'apiFetch("/api/plugins/friction/?' in page
    assert 'apiFetch("/api/plugins/friction/resolve"' in page


# ── #2595 acceptance 3: "an entry can be turned into a GitHub issue in one step" ──


def test_issue_repo_prefers_the_operators_pin(wired):
    from plugins.friction import _issue_repo

    wired.config = {"issue_repo": "protoLabsAI/protoAgent"}
    assert _issue_repo() == "protoLabsAI/protoAgent"

    wired.config = {"issue_repo": "https://github.com/protoLabsAI/protoAgent"}
    assert _issue_repo() == "protoLabsAI/protoAgent"  # a pasted URL is the same answer


def test_issue_repo_falls_back_to_the_managed_projects_registry(wired, monkeypatch):
    """ADR 0095: projects[] is "the ONE place a project is declared". Reading it beats
    reading the github plugin's own config — plugins coordinate through the host, and this
    works when that plugin isn't installed at all."""
    from plugins.friction import _issue_repo

    class _Cfg:
        projects = [{"name": "local-only", "path": "/x"},
                    {"name": "protoAgent", "github": "protoLabsAI/protoAgent"}]

    monkeypatch.setattr("graph.sdk.config", lambda: _Cfg())
    wired.config = {}

    assert _issue_repo() == "protoLabsAI/protoAgent"


def test_issue_repo_is_empty_when_nothing_declares_one(wired, monkeypatch):
    """The view must be able to tell, so it can offer the clipboard instead of a link
    that would 404."""
    from plugins.friction import _issue_repo

    class _Cfg:
        projects = []

    monkeypatch.setattr("graph.sdk.config", lambda: _Cfg())
    wired.config = {}

    assert _issue_repo() == ""


def test_the_read_api_tells_the_view_which_repo_to_file_against(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _record("harness", "filable")

    assert "issue_repo" in client.get("/api/friction").json()


def test_the_view_links_a_prefilled_issue_and_degrades_to_the_clipboard(monkeypatch):
    """One step when the repo is known; the two-step clipboard only when it isn't."""
    from pathlib import Path as _P

    from plugins import friction

    page = (_P(friction.__file__).parent / "view.html").read_text(encoding="utf-8")

    assert '"https://github.com/" + state.issueRepo + "/issues/new?"' in page
    assert 'new URLSearchParams({ title: issueTitle(item), body: issueBody(item) })' in page
    assert 'if (!state.issueRepo) return "";' in page          # no repo → no link
    assert 'target = "_blank"' in page and 'noopener noreferrer' in page
    assert "Copy as issue" in page                              # the fallback survives
