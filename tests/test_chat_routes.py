"""Chat / goal / health / OpenAI-compat routes (ADR 0023 phase 3 extraction)."""

import json
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, *, graph=object(), goal=None, chat_reply=None):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        suffix = f"@{model}" if model else ""
        return chat_reply or [{"role": "assistant", "content": f"echo:{message}{suffix}"}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", goal, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_api_chat_joins_assistant_parts(monkeypatch):
    c = _client(monkeypatch)
    body = c.post("/api/chat", json={"message": "hi"}).json()
    assert body["response"] == "echo:hi"


def test_api_chat_mints_unique_session_id_when_omitted(monkeypatch):
    # ADR 0069 D4: an omitted/blank session_id must NOT pool callers into a
    # shared "api-default" thread + memory file — each call mints a unique id
    # and the response echoes it so the caller can continue the session.
    import operator_api.chat_routes as cr

    seen: list[str] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen.append(session_id)
        return [{"role": "assistant", "content": "ok"}]

    c = _client(monkeypatch)
    monkeypatch.setattr(cr, "chat", _fake_chat)

    b1 = c.post("/api/chat", json={"message": "hi"}).json()
    b2 = c.post("/api/chat", json={"message": "hi", "session_id": "  "}).json()
    for body in (b1, b2):
        assert re.fullmatch(r"api-\d{13,}-[a-z0-9]{6}", body["session_id"])
    assert b1["session_id"] != b2["session_id"]
    assert seen == [b1["session_id"], b2["session_id"]]  # minted id reached chat()


def test_api_chat_echoes_explicit_session_id(monkeypatch):
    c = _client(monkeypatch)
    body = c.post("/api/chat", json={"message": "hi", "session_id": "tab-1"}).json()
    assert body["session_id"] == "tab-1"
    assert body["response"] == "echo:hi"


def test_api_chat_threads_per_tab_model(monkeypatch):
    c = _client(monkeypatch)
    body = c.post("/api/chat", json={"message": "hi", "model": "protolabs/fast"}).json()
    assert body["response"] == "echo:hi@protolabs/fast"  # model reached chat()


def test_api_chat_passes_incognito_flag(monkeypatch):
    # ADR 0069 D3b: `incognito` rides the request body into chat() (default
    # False — existing callers unaffected).
    import operator_api.chat_routes as cr

    seen: list[bool] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen.append(incognito)
        return [{"role": "assistant", "content": "ok"}]

    c = _client(monkeypatch)
    monkeypatch.setattr(cr, "chat", _fake_chat)

    c.post("/api/chat", json={"message": "hi"})
    c.post("/api/chat", json={"message": "hi", "incognito": True})
    assert seen == [False, True]


def test_openai_completion_honors_model_override(monkeypatch):
    c = _client(monkeypatch)
    # A real (non-agent) model id is forwarded as a per-request override.
    comp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "yo"}], "model": "protolabs/fast"},
    ).json()
    assert comp["choices"][0]["message"]["content"] == "echo:yo@protolabs/fast"
    # The agent's own advertised id means "use the configured default" (no override).
    comp2 = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "yo"}], "model": "protoagent"},
    ).json()
    assert comp2["choices"][0]["message"]["content"] == "echo:yo"


def _sse_data(text):
    """Parse an SSE body into its JSON `data:` frames (dropping the `[DONE]` sentinel)."""
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and not line.strip().endswith("[DONE]"):
            out.append(json.loads(line[len("data: ") :]))
    return out


def test_openai_completion_reports_real_usage(monkeypatch):
    # ADR 0075 D4: /v1 usage is no longer stubbed — it reflects the turn's token accounting
    # attached by server.chat (summed over lead + goal continuations + subagents).
    reply = [{"role": "assistant", "content": "hi", "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}]
    c = _client(monkeypatch, chat_reply=reply)
    body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


def test_openai_completion_usage_defaults_to_zero(monkeypatch):
    # A short-circuit / older reply carries no usage → zeros, as before (no crash).
    c = _client(monkeypatch)
    body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_openai_streaming_usage_only_when_opted_in(monkeypatch):
    reply = [{"role": "assistant", "content": "hi", "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}]

    # stream_options.include_usage → a final chunk with empty choices carrying usage.
    c = _client(monkeypatch, chat_reply=reply)
    r = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True, "stream_options": {"include_usage": True}},
    )
    frames = _sse_data(r.text)
    usage_frames = [f for f in frames if "usage" in f]
    assert usage_frames and usage_frames[-1]["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert usage_frames[-1]["choices"] == []

    # Default (no opt-in) → OpenAI omits usage from the stream.
    c2 = _client(monkeypatch, chat_reply=reply)
    r2 = c2.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert not any("usage" in f for f in _sse_data(r2.text))


def test_openai_completion_accepts_multimodal_content_list(monkeypatch):
    # #1943: OpenAI-format multimodal content (a list of text/image_url parts) must
    # not crash — text parts reach chat() as the prompt, image parts as `images`.
    import operator_api.chat_routes as cr

    seen: list[tuple] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen.append((message, images))
        return [{"role": "assistant", "content": "a red square"}]

    c = _client(monkeypatch)
    monkeypatch.setattr(cr, "chat", _fake_chat)

    data_uri = "data:image/png;base64,iVBORw0KGgo="
    body = c.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe image 1"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ]
        },
    ).json()
    assert body["choices"][0]["message"]["content"] == "a red square"
    assert seen == [("describe image 1", [("image/png", data_uri)])]


def test_openai_completion_string_content_forwards_no_images(monkeypatch):
    # Plain string content keeps the exact pre-#1943 contract: prompt as-is, images=None.
    import operator_api.chat_routes as cr

    seen: list[tuple] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen.append((message, images))
        return [{"role": "assistant", "content": "ok"}]

    c = _client(monkeypatch)
    monkeypatch.setattr(cr, "chat", _fake_chat)
    c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert seen == [("hi", None)]


def test_split_openai_content_shapes():
    # The splitter tolerates every OpenAI content shape: str, text-only list,
    # mixed parts, remote (non-data:) image URLs, and junk parts.
    from operator_api.chat_routes import _split_openai_content

    assert _split_openai_content("plain") == ("plain", [])
    assert _split_openai_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == ("a\nb", [])
    text, images = _split_openai_content(
        [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA="}},
            {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            {"type": "image_url", "image_url": {}},  # no url → skipped
            "junk",  # non-dict part → skipped
        ]
    )
    assert text == "look"
    assert images == [("image/jpeg", "data:image/jpeg;base64,AAA="), ("", "https://example.com/x.png")]
    # None / unknown shapes degrade to empty rather than crashing.
    assert _split_openai_content(None) == ("", [])


def test_delete_session_harvest_is_opt_in(monkeypatch):
    # Deleting a chat must NOT silently copy it into the knowledge base: the
    # route defaults harvest=False and forwards the dialog checkbox explicitly.
    # Both a2a: and chat: prefixes are retired; only a2a: is harvested.
    import operator_api.chat_routes as cr

    calls: list[tuple] = []

    async def _fake_retire(thread_id, *, harvest=None, cascade=True):
        calls.append((thread_id, harvest, cascade))
        return "chunk-1" if harvest else None

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    c = _client(monkeypatch)

    body = c.delete("/api/chat/sessions/s1").json()
    assert body == {"deleted": True, "harvested": False}
    body = c.delete("/api/chat/sessions/s2?harvest=true").json()
    assert body == {"deleted": True, "harvested": True}
    assert calls == [
        ("a2a:s1", False, True),
        ("chat:s1", False, True),
        ("a2a:s2", True, True),
        ("chat:s2", False, True),
    ]


def test_compact_session_route(monkeypatch):
    # The route is a thin pass-through to server.chat.compact_session — forwards the
    # path session_id and returns the compaction result dict verbatim. /compact is
    # behind the chat.compact developer flag (ADR 0068); force it ON via the env
    # override so the pass-through is what's under test, not the gate.
    import operator_api.chat_routes as cr

    monkeypatch.setenv("PROTOAGENT_FLAG_CHAT_COMPACT", "1")
    seen: list[str] = []

    async def _fake_compact(session_id):
        seen.append(session_id)
        return {
            "summary": "s",
            "archived_chunks": 2,
            "kept": 4,
            "removed": 9,
            "archived": True,
            "refused": False,
            "reason": "",
            "message": "Compacted this conversation",
        }

    monkeypatch.setattr(cr, "compact_session", _fake_compact)
    c = _client(monkeypatch)
    body = c.post("/api/chat/sessions/s1/compact").json()
    assert seen == ["s1"]
    assert body["removed"] == 9 and body["kept"] == 4 and body["archived"] is True
    assert body["message"] == "Compacted this conversation"


def test_compact_session_route_refuses_when_flag_off(monkeypatch):
    # /compact is pre-release (chat.compact developer flag, ADR 0068): on the prod
    # channel the dev-tier flag resolves OFF, the route 403s, and compact_session is
    # never reached — the checkpoint can't be touched through a disabled gate.
    import operator_api.chat_routes as cr

    monkeypatch.delenv("PROTOAGENT_FLAG_CHAT_COMPACT", raising=False)
    monkeypatch.setenv("PROTOAGENT_CHANNEL", "prod")
    called: list[str] = []

    async def _fake_compact(session_id):
        called.append(session_id)
        return {}

    monkeypatch.setattr(cr, "compact_session", _fake_compact)
    c = _client(monkeypatch)
    resp = c.post("/api/chat/sessions/s1/compact")
    assert resp.status_code == 403
    assert "chat.compact" in resp.json()["detail"]
    assert called == []


def test_rewind_session_route(monkeypatch):
    # The route is a thin pass-through to server.chat.rewind_session — forwards the
    # path session_id + body target (message_id / content / index) and returns the
    # rewind result dict verbatim.
    import operator_api.chat_routes as cr

    seen: list[dict] = []

    async def _fake_rewind(session_id, *, message_id=None, index=None, content=None, occurrence=None):
        seen.append(
            {
                "session_id": session_id,
                "message_id": message_id,
                "index": index,
                "content": content,
                "occurrence": occurrence,
            }
        )
        return {"found": True, "kept": 4, "removed": 2, "reason": "", "message": "Rewound"}

    monkeypatch.setattr(cr, "rewind_session", _fake_rewind)
    c = _client(monkeypatch)
    body = c.post(
        "/api/chat/sessions/s1/rewind", json={"message_id": "m9", "content": "the answer"}
    ).json()
    assert seen == [
        {"session_id": "s1", "message_id": "m9", "index": None, "content": "the answer", "occurrence": None}
    ]
    assert body["removed"] == 2 and body["kept"] == 4 and body["found"] is True
    assert body["message"] == "Rewound"


def test_delete_session_cleans_ephemeral_attachments(monkeypatch):
    """Deleting a chat drops its session-scoped attachment chunks."""
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_retire(thread_id, *, harvest=None, cascade=True):
        return None

    ns: list[str] = []

    class _Store:
        def delete_by_namespace(self, namespace):
            ns.append(namespace)
            return 3

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "knowledge_store", _Store(), raising=False)
    assert c.delete("/api/chat/sessions/s1").json()["deleted"] is True
    assert ns == ["attach:s1"]


def test_healthz_ready_and_echoes_ui(monkeypatch):
    c = _client(monkeypatch, graph=object())
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["graph_compiled"] is True and r.json()["ui"] == "none"


def test_healthz_503_when_graph_none(monkeypatch):
    c = _client(monkeypatch, graph=None)
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


def test_steer_enqueue_then_cancel_roundtrip(monkeypatch):
    # Full HTTP lifecycle of the steer endpoints: POST queues, GET peeks, DELETE
    # cancels a still-queued message, and a second DELETE reports too-late.
    from graph import steering

    steering._QUEUES.clear()
    c = _client(monkeypatch)
    posted = c.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "do X instead"}).json()
    assert posted == {"ok": True, "id": "m1", "pending": 1}
    assert c.get("/api/chat/sessions/s1/steer").json() == {"pending": [{"id": "m1", "text": "do X instead"}]}

    # ✕ before the turn folds it in → removed, queue empties.
    assert c.delete("/api/chat/sessions/s1/steer/m1").json() == {"removed": True, "pending": 0}
    # ✕ again (or after it's drained) → too late, nothing removed.
    assert c.delete("/api/chat/sessions/s1/steer/m1").json() == {"removed": False, "pending": 0}
    steering._QUEUES.clear()


def test_delegation_list_and_cancel_roundtrip(monkeypatch):
    # HTTP lifecycle of the Tier 2 delegation routes: GET lists in-flight `task`
    # delegations; POST cancels one (the route hits graph.delegations directly).
    from graph import delegations

    class _Fake:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return self.cancelled

        def cancel(self):
            self.cancelled = True

    delegations._RUNNING.clear()
    c = _client(monkeypatch)
    assert c.get("/api/chat/sessions/s1/delegations").json() == {"running": []}

    f = _Fake()
    delegations.register("s1", "d1", f, label="research X")
    assert c.get("/api/chat/sessions/s1/delegations").json() == {
        "running": [{"id": "d1", "label": "research X"}]
    }
    # Cancel the live delegation → cancelled; still counted (the tool's finally
    # unregisters once the task actually unwinds, not the route).
    assert c.post("/api/chat/sessions/s1/delegations/d1/cancel").json() == {"cancelled": True, "running": 1}
    assert f.cancelled is True
    # Cancel again (already cancelling) and an unknown id → both too-late/false.
    assert c.post("/api/chat/sessions/s1/delegations/d1/cancel").json() == {"cancelled": False, "running": 1}
    assert c.post("/api/chat/sessions/s1/delegations/nope/cancel").json() == {"cancelled": False, "running": 1}
    delegations._RUNNING.clear()


def test_openai_models_and_completion(monkeypatch):
    c = _client(monkeypatch)
    models = c.get("/v1/models").json()
    assert models["data"][0]["id"] == "protoagent"
    comp = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "yo"}]}).json()
    assert comp["choices"][0]["message"]["content"] == "echo:yo"
    assert comp["model"] == "protoagent"


def test_openai_completions_threads_incognito(monkeypatch):
    # /v1 must expose incognito (ADR 0069) like /api/chat + A2A do, so a programmatic
    # caller (eval/benchmark harness) can run a turn with no memory injection.
    import operator_api.chat_routes as cr

    seen: list[bool] = []

    async def _fake_chat(message, session_id, *, model=None, incognito=False, hitl_resume=False, images=None):
        seen.append(incognito)
        return [{"role": "assistant", "content": "ok"}]

    c = _client(monkeypatch)
    monkeypatch.setattr(cr, "chat", _fake_chat)

    c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "incognito": True})
    assert seen == [False, True]  # default off (unchanged); opt-in honored


def test_openai_streaming(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "yo"}], "stream": True})
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    first = json.loads(frames[0][len("data: ") :])
    assert first["choices"][0]["delta"]["content"] == "echo:yo"
    assert frames[-1] == "data: [DONE]"


def test_export_session_route(monkeypatch):
    # Thin pass-through to server.chat.export_session — forwards the path session_id
    # plus the optional ?title, and returns the export result dict verbatim. No
    # developer-flag gate: export is read-only (unlike /compact), so there's nothing
    # to guard behind a pre-release flag.
    import operator_api.chat_routes as cr

    seen: list[tuple] = []

    async def _fake_export(session_id, *, title=None):
        seen.append((session_id, title))
        return {
            "found": True,
            "markdown": "# Chat export\n\n## User\n\nhi\n",
            "message_count": 2,
            "redactions": ["openai-key"],
            "reason": "ok",
            "message": "Exported 2 message(s) as Markdown.",
        }

    monkeypatch.setattr(cr, "export_session", _fake_export)
    c = _client(monkeypatch)
    body = c.get("/api/chat/sessions/s1/export?title=My%20Chat").json()
    assert seen == [("s1", "My Chat")]
    assert body["found"] is True and body["message_count"] == 2
    assert body["markdown"].startswith("# Chat export")
    # The redaction summary must survive to the caller — it's what the operator
    # reviews before sharing.
    assert body["redactions"] == ["openai-key"]
