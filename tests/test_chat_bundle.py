"""Tests for graph.chat_bundle — the structured chat-bundle export (#2680/#2681).

Mirrors tests/test_export_op.py's two-layer shape: the pure halves (``build_bundle`` /
``build_bundle_zip``) exercised directly with fake messages and a fake artifact resolver,
then ``export_bundle`` against a fake graph so no host or checkpointer is needed.
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph.chat_bundle import build_bundle, build_bundle_zip, export_bundle


# ── build_bundle: shape + redaction ─────────────────────────────────────────────
def test_excludes_system_messages():
    manifest, _ = build_bundle(
        [SystemMessage(content="SECRET SYSTEM PROMPT"), HumanMessage(content="hi")], thread_id="t1"
    )
    assert manifest["bundle_version"] == 1
    assert len(manifest["messages"]) == 1
    assert manifest["messages"][0]["role"] == "user"
    text = json.dumps(manifest)
    assert "SECRET SYSTEM PROMPT" not in text


def test_tool_result_is_folded_into_the_calling_tool_call_not_a_separate_message():
    manifest, _ = build_bundle(
        [
            HumanMessage(content="what's the weather"),
            AIMessage(content="checking", tool_calls=[{"name": "get_weather", "args": {"city": "NYC"}, "id": "c1"}]),
            ToolMessage(content="72F and sunny", tool_call_id="c1"),
        ],
        thread_id="t1",
    )
    # No third "tool"-role message — matches the console's ChatMessage.toolCalls model.
    assert [m["role"] for m in manifest["messages"]] == ["user", "assistant"]
    assistant = manifest["messages"][1]
    text_parts = [p for p in assistant["parts"] if p["kind"] == "text"]
    call_parts = [p for p in assistant["parts"] if p["kind"] == "tool_call"]
    assert text_parts[0]["text"] == "checking"
    assert call_parts[0]["name"] == "get_weather"
    assert call_parts[0]["input"] == {"city": "NYC"}
    assert call_parts[0]["output"] == "72F and sunny"


def test_redacts_text_and_tool_output_and_reports_kinds():
    manifest, redactions = build_bundle(
        [
            HumanMessage(content="my key is sk-abcdefghijklmnopqrstuvwxyz123456"),
            AIMessage(content="", tool_calls=[{"name": "run", "args": {}, "id": "c1"}]),
            ToolMessage(content="token ghp_abcdefghijklmnopqrstuvwxyz0123", tool_call_id="c1"),
        ],
        thread_id="t1",
    )
    text = json.dumps(manifest)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in text
    assert set(redactions) == {"openai-key", "github-token"}


def test_redaction_can_be_disabled():
    manifest, redactions = build_bundle(
        [HumanMessage(content="sk-abcdefghijklmnopqrstuvwxyz123456")], thread_id="t1", redact_secrets=False
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" in json.dumps(manifest)
    assert redactions == []


def test_empty_message_is_dropped():
    manifest, _ = build_bundle([AIMessage(content="")], thread_id="t1")
    assert manifest["messages"] == []


def test_multipart_content_flattened_like_export_op():
    manifest, _ = build_bundle(
        [HumanMessage(content=[{"type": "text", "text": "look"}, {"type": "image_url"}])], thread_id="t1"
    )
    text = manifest["messages"][0]["parts"][0]["text"]
    assert text == "look\n\n_[image_url]_"


# ── artifact resolution ──────────────────────────────────────────────────────────
def _ai_and_tool(name, args, result, call_id="c1"):
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}]),
        ToolMessage(content=result, tool_call_id=call_id),
    ]


def test_show_artifact_uses_call_args_directly_never_the_resolver():
    """The call's own args carry the full code — no store round-trip needed, so a
    resolver that would blow up must never be invoked for this tool."""

    def _boom(_id, _version):
        raise AssertionError("resolver must not be called for show_artifact")

    messages = _ai_and_tool(
        "show_artifact",
        {"kind": "html", "code": "<h1>Hi</h1>", "title": "T"},
        "Created html artifact a-1700000000000-abcdef (11 chars) — now showing in the Artifact panel.",
    )
    manifest, _ = build_bundle(messages, thread_id="t1", artifact_resolver=_boom)
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert artifact == {
        "id": "a-1700000000000-abcdef",
        "artifact_kind": "html",
        "title": "T",
        "version": 1,
        "available": True,
        "content": "<h1>Hi</h1>",
    }


def test_update_artifact_consults_the_resolver_for_the_merged_text():
    calls = []

    def resolver(art_id, version):
        calls.append((art_id, version))
        return {"available": True, "kind": "html", "title": "T", "version": 2, "code": "<h1>World</h1>"}

    messages = _ai_and_tool(
        "update_artifact",
        {"old_string": "Hello", "new_string": "World"},
        "Updated artifact a-1700000000000-abcdef → version 2.",
    )
    manifest, _ = build_bundle(messages, thread_id="t1", artifact_resolver=resolver)
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert calls == [("a-1700000000000-abcdef", 2)]
    assert artifact["available"] is True
    assert artifact["content"] == "<h1>World</h1>"


def test_unavailable_artifact_carries_reason_not_content():
    def resolver(_id, _version):
        return {"available": False, "kind": "html", "title": "T", "reason": "referenced version is no longer available"}

    messages = _ai_and_tool(
        "update_artifact", {"old_string": "a", "new_string": "b"}, "Updated artifact a-1700000000001-abcdef → version 5."
    )
    manifest, _ = build_bundle(messages, thread_id="t1", artifact_resolver=resolver)
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert artifact["available"] is False
    assert "content" not in artifact
    assert "no longer available" in artifact["reason"]


def test_file_artifact_carries_file_meta_not_bytes():
    def resolver(_id, _version):
        return {
            "available": False,
            "kind": "file",
            "title": "Report",
            "reason": "binary attachment not included in this export",
            "file_meta": {"filename": "report.pdf", "mime": "application/pdf", "size": 2048},
        }

    messages = _ai_and_tool(
        "save_file_artifact", {"path": "/tmp/report.pdf"}, "Saved file artifact a-1700000000001-abcdef → v1: report.pdf (application/pdf, 2 KB)"
    )
    manifest, _ = build_bundle(messages, thread_id="t1", artifact_resolver=resolver)
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert artifact["available"] is False
    assert artifact["file_meta"]["filename"] == "report.pdf"


def test_artifact_content_is_redacted_too():
    messages = _ai_and_tool(
        "show_artifact",
        {"kind": "html", "code": "<p>sk-abcdefghijklmnopqrstuvwxyz123456</p>"},
        "Created html artifact a-1700000000001-abcdef (30 chars) — now showing in the Artifact panel.",
    )
    manifest, redactions = build_bundle(messages, thread_id="t1")
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in artifact["content"]
    assert "openai-key" in redactions


def test_non_artifact_tool_call_has_no_artifact_field():
    messages = _ai_and_tool("get_weather", {"city": "NYC"}, "72F and sunny")
    manifest, _ = build_bundle(messages, thread_id="t1")
    assert "artifact" not in manifest["messages"][0]["parts"][0]


def test_unparseable_result_text_degrades_to_no_artifact_field():
    """A future wording change in the artifact plugin must not crash the export — just
    lose the enrichment for that one call."""
    messages = _ai_and_tool("show_artifact", {"kind": "html", "code": "<x/>"}, "some totally different wording")
    manifest, _ = build_bundle(messages, thread_id="t1")
    assert "artifact" not in manifest["messages"][0]["parts"][0]


def test_no_resolver_defaults_to_unavailable_not_a_crash():
    messages = _ai_and_tool(
        "update_artifact", {"old_string": "a", "new_string": "b"}, "Updated artifact a-1700000000001-abcdef → version 2."
    )
    manifest, _ = build_bundle(messages, thread_id="t1")  # no artifact_resolver passed
    artifact = manifest["messages"][0]["parts"][0]["artifact"]
    assert artifact["available"] is False


# ── build_bundle_zip ─────────────────────────────────────────────────────────────
def test_zip_contains_manifest_and_review():
    manifest, redactions = build_bundle([HumanMessage(content="hi")], thread_id="t1", title="My Chat")
    result = build_bundle_zip(manifest, redactions)
    zf = zipfile.ZipFile(BytesIO(result.data))
    assert set(zf.namelist()) == {"manifest.json", "REVIEW.md"}
    assert json.loads(zf.read("manifest.json").decode())["title"] == "My Chat"
    review = zf.read("REVIEW.md").decode()
    assert "Chat bundle review — My Chat" in review
    assert "not a guarantee" in review


def test_review_discloses_redactions():
    manifest, redactions = build_bundle(
        [HumanMessage(content="key sk-abcdefghijklmnopqrstuvwxyz123456")], thread_id="t1"
    )
    result = build_bundle_zip(manifest, redactions)
    review = zipfile.ZipFile(BytesIO(result.data)).read("REVIEW.md").decode()
    assert "openai-key" in review
    assert result.redactions == ["openai-key"]


def test_review_discloses_unavailable_artifacts():
    messages = _ai_and_tool(
        "update_artifact", {"old_string": "a", "new_string": "b"}, "Updated artifact a-1700000000001-abcdef → version 5."
    )
    manifest, redactions = build_bundle(messages, thread_id="t1")  # no resolver → unavailable
    result = build_bundle_zip(manifest, redactions)
    review = zipfile.ZipFile(BytesIO(result.data)).read("REVIEW.md").decode()
    assert "Artifacts not fully included" in review
    assert "a-1700000000001-abcdef" in review
    assert result.artifact_notes


def test_review_is_clean_when_nothing_was_scrubbed_or_missing():
    manifest, redactions = build_bundle([HumanMessage(content="hello there")], thread_id="t1")
    result = build_bundle_zip(manifest, redactions)
    review = zipfile.ZipFile(BytesIO(result.data)).read("REVIEW.md").decode()
    assert "None — nothing matched a known secret shape" in review
    assert "None — every artifact this thread referenced is included" in review


# ── export_bundle (the op) ────────────────────────────────────────────────────────
class _FakeSnapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages
        self.updated = False

    async def aget_state(self, _config):
        return _FakeSnapshot(self._messages)

    async def aupdate_state(self, *_a, **_k):  # pragma: no cover - must never run
        self.updated = True
        raise AssertionError("export must never mutate the checkpoint")


def test_export_bundle_returns_manifest():
    graph = _FakeGraph([HumanMessage(content="hello"), AIMessage(content="hi there")])
    out = asyncio.run(export_bundle(graph, object(), "t1"))
    assert out["found"] is True and out["reason"] == "ok"
    assert out["message_count"] == 2
    assert out["manifest"]["thread_id"] == "t1"
    assert graph.updated is False


def test_export_bundle_empty_thread():
    graph = _FakeGraph([])
    out = asyncio.run(export_bundle(graph, object(), "t1"))
    assert out["found"] is False and out["reason"] == "empty_thread" and out["manifest"] is None


def test_export_bundle_no_checkpointer():
    out = asyncio.run(export_bundle(None, None, "t1"))
    assert out["found"] is False and out["reason"] == "no_checkpointer"


@pytest.mark.parametrize("thread_id", ["t1"])
def test_export_bundle_is_read_only_even_with_secrets(thread_id):
    graph = _FakeGraph([HumanMessage(content="key sk-abcdefghijklmnopqrstuvwxyz123456")])
    out = asyncio.run(export_bundle(graph, object(), thread_id))
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(out["manifest"])
    assert out["redactions"] == ["openai-key"]
    assert graph.updated is False
