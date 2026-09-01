"""Tests for the friction plugin — record/review + auto-capture middleware."""

from __future__ import annotations

import asyncio
import json

import pytest

from plugins.friction import FrictionMiddleware, friction_review, record_friction, resolve_friction


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
    app.include_router(friction._build_router())
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
    app.include_router(friction._build_router())
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
    app.include_router(friction._build_router())
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
    """The plugin with a registry attached, as the host wires it at boot."""
    from plugins import friction

    monkeypatch.setenv("FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    friction._WORK_CACHE["stamp"] = None  # module-level cache, per-test

    class _Registry:
        plugin_id = "friction"

        def __init__(self):
            self.events: list[tuple[str, dict]] = []
            self.work_providers: dict = {}
            self.skill_dirs: list = []
            self.cfg: dict = {}

        def emit(self, topic, data=None):
            self.events.append((topic, data or {}))

        def live_config(self):
            return self.cfg

        def register_work_provider(self, name, fn, label=""):
            self.work_providers[name] = (fn, label)

        def register_skill_dir(self, path):
            self.skill_dirs.append(path)

        def register_tools(self, tools):
            pass

        def register_middleware(self, factory):
            pass

        def register_router(self, router, prefix=None):
            pass

    reg = _Registry()
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

    wired.cfg = {"working_state_limit": 5}
    assert len(open_friction_work()) == 5
    wired.cfg = {"working_state": False}
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
    assert wired.work_providers["backlog"][1] == "OPEN FRICTION"
    assert wired.skill_dirs == ["skills"]


# ── ADR 0039 seam: friction is broadcast, so other plugins can act on it ─────


def test_recording_and_resolving_emit_on_the_bus(wired):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "worth hearing", "severity": "major"}))

    topic, data = wired.events[-1]
    assert topic == "recorded"
    assert data["summary"] == "worth hearing" and data["severity"] == "major" and data["kind"] == "harness"

    asyncio.run(resolve_friction.ainvoke({"summary": "worth hearing", "reason": "shipped"}))
    assert wired.events[-1][0] == "resolved"
    assert wired.events[-1][1]["count"] == 1 and wired.events[-1][1]["reason"] == "shipped"


def test_a_bus_failure_never_breaks_a_tool_call(wired):
    """Friction recording is the system's self-improvement path; it must not be the thing
    that fails a turn."""
    def _boom(topic, data=None):
        raise RuntimeError("bus down")

    wired.emit = _boom
    out = asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "still logged"}))

    assert "logged" in out
    assert _recs(_ledger_for(wired))[0]["summary"] == "still logged"


def _ledger_for(_registry):
    from plugins.friction import _ledger_path

    return _ledger_path()
