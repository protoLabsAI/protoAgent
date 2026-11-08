"""A refused tool call counts as an error in per-tool telemetry.

The v0.231.0 stress test found ``GET /api/telemetry/tools`` reporting ``cite_artifact``
at 7 calls / 0 errors although 3 calls were refusals, and ``knowledge_ingest`` at 0 errors
although one returned ``No such file: ...``. The audit middleware decided success from an
``"Error"`` text prefix, and those refusals don't start with it.

The standard failure signal is the ToolMessage's ``status="error"``: a tool raises
``ToolException`` and its ``handle_tool_error`` turns that into a result with the same
words and ``status="error"`` (the same signal the chat card closes as an X on). These
tests pin both halves: the middleware reads the status, and the refusing tools set it,
through a real tool call (not a stand-in result), while the words the model reads stay
exactly what they were.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from graph.middleware import audit
from observability import tracing
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE
from tests.test_artifact_cite import _save
from tests.test_artifact_linked import _load
from tools.lg_tools import get_all_tools


def _drain():
    audit._TELEMETRY_WRITER.submit(lambda: None).result(timeout=10)


def _rows(store):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT tool, ok, error_class FROM tool_calls")]


class _Metrics:
    calls: list = []

    @classmethod
    def record_tool_call(cls, name, success, _seconds):
        cls.calls.append((name, success))


class _Audit:
    rows: list = []

    @classmethod
    def log(cls, **kw):
        cls.rows.append(kw)


@pytest.fixture
def telemetry(tmp_path, monkeypatch):
    _drain()
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(STATE, "telemetry_store", store)
    monkeypatch.setattr(STATE, "ledger_store", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_meta", [])
    monkeypatch.setattr(audit, "_TELEMETRY_FAILURE_LOGGED", False)
    monkeypatch.setattr(_Metrics, "calls", [])
    monkeypatch.setattr(_Audit, "rows", [])
    monkeypatch.setattr(audit, "_observability", lambda: (_Audit, tracing, _Metrics))
    yield store
    _drain()


def _call(name, args, call_id="call-1"):
    return {"type": "tool_call", "name": name, "args": args, "id": call_id}


# ── the middleware reads status="error", whatever the words ───────────────────────────────


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_status_error_result_counts_as_an_error(telemetry, mode):
    request = SimpleNamespace(tool_call={"name": "sample", "args": {}})
    result = ToolMessage(content="No such file: stress-note-1", tool_call_id="c", status="error")
    mw = audit.AuditMiddleware()
    if mode == "sync":
        out = mw.wrap_tool_call(request, lambda _r: result)
    else:

        async def handler(_r):
            return result

        out = await mw.awrap_tool_call(request, handler)
    assert out is result
    assert out.content == "No such file: stress-note-1"  # the model's text is untouched
    _drain()
    assert _rows(telemetry) == [{"tool": "sample", "ok": 0, "error_class": "ToolError"}]
    # The audit log and Prometheus counter agree with the telemetry row.
    assert _Audit.rows[-1]["success"] is False
    assert _Metrics.calls == [("sample", False)]


async def test_success_status_and_legacy_error_prefix_still_read_correctly(telemetry):
    mw = audit.AuditMiddleware()
    ok = ToolMessage(content="Cited artifact a-1 v1", tool_call_id="c")  # status defaults to "success"
    legacy = ToolMessage(content="Error: knowledge is not available", tool_call_id="c")
    for name, res in (("good", ok), ("legacy", legacy)):
        mw.wrap_tool_call(SimpleNamespace(tool_call={"name": name, "args": {}}), lambda _r, res=res: res)
    _drain()
    by_tool = {r["tool"]: r for r in _rows(telemetry)}
    assert by_tool["good"]["ok"] == 1 and by_tool["good"]["error_class"] is None
    assert by_tool["legacy"]["ok"] == 0


# ── cite_artifact's refusals: same words, status="error", counted ─────────────────────────


@pytest.fixture
def art(monkeypatch, tmp_path):
    from graph import components

    mod = _load(monkeypatch, tmp_path / "store")
    components.set_plugin_components({mod._ref.ARTIFACT_REF: mod._ref.validate_artifact_ref})
    yield mod
    components.set_plugin_components(None)


def test_cite_artifact_refusals_are_status_error_with_unchanged_text(art, monkeypatch, tmp_path):
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})  # one version
    cases = [
        ({"artifact_id": saved["id"], "page": -1}, "page must be a positive PDF page number"),
        ({"artifact_id": saved["id"], "page": 1, "version": 5}, f"Artifact {saved['id']} has 1 version(s)"),
        ({"artifact_id": "a-doesnotexist", "page": 1}, "No artifact 'a-doesnotexist'"),
    ]
    for args, words in cases:
        msg = art.cite_artifact.invoke(_call("cite_artifact", args))
        assert isinstance(msg, ToolMessage)
        assert msg.status == "error", args
        assert msg.content.startswith(words), msg.content
        # A plain (non-tool-call) invoke still returns exactly the same text.
        assert art.cite_artifact.invoke(args) == msg.content

    good = art.cite_artifact.invoke(_call("cite_artifact", {"artifact_id": saved["id"], "page": 2}))
    assert good.status == "success"


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("get_artifact", {"artifact_id": "a-nope"}),
        ("check_artifact", {"artifact_id": "a-nope"}),
        ("pin_artifact", {"artifact_id": "a-nope"}),
        ("delete_artifact", {"artifact_id": "a-nope"}),
    ],
)
def test_other_artifact_not_found_refusals_are_status_error(art, tool_name, args):
    tool = getattr(art, tool_name)
    msg = tool.invoke(_call(tool_name, args))
    assert msg.status == "error"
    assert msg.content.startswith("No artifact")
    assert tool.invoke(args) == msg.content


def test_cite_artifact_refusals_count_in_per_tool_telemetry(art, monkeypatch, tmp_path, telemetry):
    """The stress-test repro, end to end: three refusals and one success through the
    audit middleware around the real tool call → 4 calls, 3 errors."""
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})
    mw = audit.AuditMiddleware()
    calls = [
        {"artifact_id": saved["id"], "page": -1},
        {"artifact_id": saved["id"], "page": 1, "version": 5},
        {"artifact_id": "a-doesnotexist", "page": 1},
        {"artifact_id": saved["id"], "page": 2},
    ]
    for i, args in enumerate(calls):
        tc = _call("cite_artifact", args, call_id=f"c{i}")
        mw.wrap_tool_call(SimpleNamespace(tool_call=tc), lambda req: art.cite_artifact.invoke(req.tool_call))
    _drain()
    oks = [r["ok"] for r in _rows(telemetry)]
    assert sorted(oks) == [0, 0, 0, 1]


# ── knowledge_ingest's "No such file": same words, status="error", counted ────────────────


class _FakeStore:
    def add_document(self, content, **kw):
        return [1]


def _ingest_tool():
    return {t.name: t for t in get_all_tools(_FakeStore())}["knowledge_ingest"]


async def test_knowledge_ingest_missing_file_is_status_error(telemetry):
    tool = _ingest_tool()
    msg = await tool.ainvoke(_call("knowledge_ingest", {"source": "stress-note-1"}))
    assert isinstance(msg, ToolMessage)
    assert msg.status == "error"
    assert msg.content.startswith("No such file: stress-note-1")
    assert await tool.ainvoke({"source": "stress-note-1"}) == msg.content  # same words as before

    async def handler(req):
        return await tool.ainvoke(req.tool_call)

    tc = _call("knowledge_ingest", {"source": "stress-note-1"})
    await audit.AuditMiddleware().awrap_tool_call(SimpleNamespace(tool_call=tc), handler)
    _drain()
    assert _rows(telemetry) == [{"tool": "knowledge_ingest", "ok": 0, "error_class": "ToolError"}]


async def test_knowledge_ingest_empty_source_is_status_error():
    msg = await _ingest_tool().ainvoke(_call("knowledge_ingest", {"source": "   "}))
    assert msg.status == "error"
    assert msg.content == "Error: provide a URL or a local file path to ingest."
