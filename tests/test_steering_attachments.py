"""Steer HTTP ingress persists attachments and folds them at the model boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from graph import steering
from graph.middleware.steering import SteeringMiddleware, _INTERJECTION
from runtime.state import STATE


@pytest.fixture
def client(monkeypatch, tmp_path):
    from operator_api.chat_routes import register_chat_routes

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    steering._reset()
    app = FastAPI()
    register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        yield client
    steering._reset()


@pytest.mark.parametrize("vision", [True, False, None])
@pytest.mark.parametrize("text", ["look", ""])
def test_route_persists_and_folds_images(client, monkeypatch, vision, text):
    import infra.media as media
    from server.chat import _ATTACHMENT_REFS_MARKER

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=vision) if vision is not None else None)
    save = Mock(wraps=media.save_media)
    monkeypatch.setattr(media, "save_media", save)
    images = [["image/png", "data:image/png;base64,AAA="]]
    response = client.post("/api/chat/sessions/attachments/steer", json={"id": "a", "text": text, "images": images})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": "a", "pending": 1}
    item = steering.pending_items("attachments")[0]
    # The route normalizes the wire lists to the [(media_type, uri)] tuple shape.
    assert item["images"] == [tuple(img) for img in images]
    assert item["vision"] is bool(vision)
    save.assert_called_once_with(b"\x00\x00", "image/png", {"source": "user_attachment", "session_id": "attachments"})
    stored = list(media.media_dir().glob("*.png"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == b"\x00\x00"
    assert _ATTACHMENT_REFS_MARKER in item["refs"]
    assert stored[0].stem in item["refs"]

    # The decision belongs to the queued item, even if config changes before drain.
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=not vision))
    seen = []
    monkeypatch.setattr("langchain_core.callbacks.dispatch_custom_event", lambda name, data: seen.append((name, data)))
    update = SteeringMiddleware().before_model({"session_id": "attachments"}, None)
    human = update["messages"][0]
    assert isinstance(human, HumanMessage)
    assert human.content[0] == {"type": "text", "text": _INTERJECTION + text + "\n\n" + item["refs"]}
    assert human.content[1:] == ([{"type": "image_url", "image_url": {"url": images[0][1]}}] if vision else [])
    assert seen == [("steer_consumed", {"items": [{"id": "a", "text": text}]})]
    assert steering.pending("attachments") == 0
    assert SteeringMiddleware._inject({"session_id": "attachments"}) is None
    assert save.call_count == 1


@pytest.mark.parametrize("extra", [{}, {"images": []}, {"images": None}])
def test_text_only_route_preserves_item_and_message(client, monkeypatch, extra):
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    for mid, text in [("a", " first "), ("b", "second")]:
        response = client.post("/api/chat/sessions/text-only/steer", json={"id": mid, "text": text, **extra})
        assert response.status_code == 200
    expected = [{"id": "a", "text": "first"}, {"id": "b", "text": "second"}]
    assert steering.pending_items("text-only") == expected
    update, consumed = SteeringMiddleware._drain({"session_id": "text-only"})
    assert update["messages"][0].content == _INTERJECTION + "first\n\nsecond"
    assert consumed == expected


def test_mixed_fold_keeps_per_item_vision_and_fences(client, monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=False))
    url = "https://example.com/image.png"
    assert client.post("/api/chat/sessions/mixed/steer", json={
        "id": "a", "text": "hidden", "images": [["image/png", url]],
    }).status_code == 200
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    assert client.post("/api/chat/sessions/mixed/steer", json={
        "id": "b", "text": "visible", "images": [["image/png", url]],
    }).status_code == 200
    steering.enqueue("mixed", "fenced", msg_id="c", fence=["read_file"])
    update, consumed = SteeringMiddleware._drain({"session_id": "mixed"})
    assert update["messages"][0].content == [
        {"type": "text", "text": _INTERJECTION + "hidden\n\nvisible\n\nfenced"},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    assert update["subagent_fence"] == ["read_file"]
    assert consumed == [{"id": "a", "text": "hidden"}, {"id": "b", "text": "visible"}, {"id": "c", "text": "fenced"}]


def test_unsafe_image_is_omitted_but_refs_survive(client, monkeypatch):
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    monkeypatch.setattr("graph.image_limits.fit_data_uri", lambda uri: None)
    assert client.post("/api/chat/sessions/oversized/steer", json={
        "text": "look", "images": [["image/png", "data:image/png;base64,AAA="]],
    }).status_code == 200
    content = SteeringMiddleware._inject({"session_id": "oversized"})["messages"][0].content
    assert "[attached-image refs]" in content[0]["text"]
    assert content[1] == {"type": "text", "text": "[an attached image was omitted: too large to send to the model]"}


@pytest.mark.parametrize("text", ["look", ""])
def test_incognito_route_does_not_persist_but_image_still_folds(client, monkeypatch, text):
    import infra.media as media
    from server.chat import _ATTACHMENT_REFS_MARKER

    # ADR 0069 D3b: an incognito turn leaves no durable trace. The steer route mirrors
    # _vision_human_message — it skips the media bridge (and the refs note) on incognito,
    # so nothing is saved, but the image still reaches a vision model as a block.
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    save = Mock(wraps=media.save_media)
    monkeypatch.setattr(media, "save_media", save)
    images = [["image/png", "data:image/png;base64,AAA="]]
    response = client.post(
        "/api/chat/sessions/incognito/steer",
        json={"id": "a", "text": text, "images": images, "incognito": True},
    )
    assert response.status_code == 200
    save.assert_not_called()
    item = steering.pending_items("incognito")[0]
    assert item["refs"] == ""
    assert item["images"] == [tuple(img) for img in images] and item["vision"] is True
    content = SteeringMiddleware._inject({"session_id": "incognito"})["messages"][0].content
    assert content[0] == {"type": "text", "text": _INTERJECTION + text}
    assert content[1] == {"type": "image_url", "image_url": {"url": images[0][1]}}
    assert _ATTACHMENT_REFS_MARKER not in content[0]["text"]


@pytest.mark.parametrize(
    "bad",
    ["not-a-list", 5, {"a": 1}, [["only-one"]], ["bare"], [{"type": "image_url"}], [["a", "b", "c"]]],
)
def test_malformed_images_do_not_500_and_fall_back_to_text_only(client, monkeypatch, bad):
    import infra.media as media

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    save = Mock(wraps=media.save_media)
    monkeypatch.setattr(media, "save_media", save)
    response = client.post("/api/chat/sessions/bad/steer", json={"id": "x", "text": "steer", "images": bad})
    assert response.status_code == 200
    save.assert_not_called()
    # Nothing usable in ``images`` → exactly today's text-only queue item and fold.
    assert steering.pending_items("bad") == [{"id": "x", "text": "steer"}]
    update, _consumed = SteeringMiddleware._drain({"session_id": "bad"})
    assert update["messages"][0].content == _INTERJECTION + "steer"


def test_turn_query_reads_text_blocks_not_base64(client, monkeypatch):
    from graph.middleware.knowledge import turn_query

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=True))
    data_uri = "data:image/png;base64,AAA="
    assert client.post(
        "/api/chat/sessions/query/steer",
        json={"text": "describe the chart", "images": [["image/png", data_uri]]},
    ).status_code == 200
    content = SteeringMiddleware._inject({"session_id": "query"})["messages"][0].content
    assert isinstance(content, list)  # multimodal: text block + image block
    q = turn_query([HumanMessage(content=content)])
    assert "describe the chart" in q
    assert data_uri not in q  # base64 never reaches the retrieval embedder
    assert not q.startswith("[User message received")  # interjection frame is stripped


@pytest.mark.parametrize("vision", [True, False])
def test_posted_attachment_reaches_model_call(client, monkeypatch, vision):
    from langchain.agents import create_agent
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from graph.state import ProtoAgentState

    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(model_vision=vision))
    seen = []

    class Capture(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            seen.extend(messages[0])

    agent = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="acknowledged")])),
        middleware=[SteeringMiddleware()],
        state_schema=ProtoAgentState,
    )
    response = client.post("/api/chat/sessions/model-call/steer", json={
        "text": "use this image", "images": [["image/png", "data:image/png;base64,AAA="]],
    })
    assert response.status_code == 200
    refs = steering.pending_items("model-call")[0]["refs"]
    agent.invoke(
        {"messages": [HumanMessage(content="original task")], "session_id": "model-call"},
        config={"callbacks": [Capture()]},
    )
    assert seen[-1].content == [
        {"type": "text", "text": _INTERJECTION + "use this image\n\n" + refs},
        *([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}}] if vision else []),
    ]


@pytest.mark.parametrize("images", [None, [], [["only-one"]], "invalid"])
def test_empty_steer_requires_usable_images(client, images):
    response = client.post("/api/chat/sessions/empty/steer", json={"text": "  ", "images": images})
    assert response.status_code == 400
    assert steering.pending("empty") == 0
