"""Multimodal tool results (#1930) — envelope, guardrails, and the middleware rewrite.

The riskiest property here is the NON-multimodal path: the middleware runs on
every tool call, so an ordinary string-returning tool must come through as the
same untouched object. The rest covers the opt-in path: envelope → image_url
content blocks on a vision model, graceful degradation (optionally via the
describe fallback) on a text-only model, and the count/byte caps.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from graph.multimodal import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_RESULT,
    _SENTINEL,
    is_multimodal_result,
    multimodal_tool_result,
    parse_multimodal_result,
    render_multimodal_content,
)
from graph.middleware.multimodal_tool import MultimodalToolResultMiddleware, build_multimodal_middleware

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def _envelope(text="a chart", n=1, mime="image/png"):
    return multimodal_tool_result(text, [{"b64": PNG_B64, "mime": mime}] * n)


def _req(name="paint", call_id="c1"):
    return SimpleNamespace(tool_call={"name": name, "args": {}, "id": call_id})


# ── envelope helper ───────────────────────────────────────────────────────────


def test_helper_builds_sentinel_envelope_and_parses_back():
    out = _envelope("one image + caption")
    assert isinstance(out, str) and out.startswith(_SENTINEL)
    env = parse_multimodal_result(out)
    assert env["text"] == "one image + caption"
    assert env["images"] == [{"b64": PNG_B64, "mime": "image/png"}]


def test_helper_reads_path_images(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(PNG_BYTES)
    env = parse_multimodal_result(multimodal_tool_result("from disk", [{"path": str(p)}]))
    assert env["images"][0]["b64"] == PNG_B64
    assert env["images"][0]["mime"] == "image/png"


def test_helper_rejects_bad_input_eagerly():
    with pytest.raises(ValueError, match="at least one image"):
        multimodal_tool_result("t", [])
    with pytest.raises(ValueError, match="too many images"):
        _envelope(n=MAX_IMAGES_PER_RESULT + 1)
    with pytest.raises(ValueError, match="non-image mime"):
        _envelope(mime="text/html")
    with pytest.raises(ValueError, match="not valid base64"):
        multimodal_tool_result("t", [{"b64": "!!not-base64!!"}])
    big = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ValueError, match="downscale"):
        multimodal_tool_result("t", [{"b64": big}])


def test_parse_ignores_ordinary_results():
    for content in ("plain string", "", '{"text": "json but no sentinel"}', None, 42, ["blocks"]):
        assert parse_multimodal_result(content) is None
        assert not is_multimodal_result(content)


def test_parse_degrades_malformed_payload_instead_of_keeping_the_blob():
    env = parse_multimodal_result(_SENTINEL + "{not json")
    assert env["images"] == [] and "malformed" in env["text"]


# ── rendering: vision path ────────────────────────────────────────────────────


def test_vision_renders_text_plus_image_url_blocks():
    env = parse_multimodal_result(_envelope("look at this", n=2))
    blocks = render_multimodal_content(env, vision=True)
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "look at this"}
    assert [b["type"] for b in blocks[1:]] == ["image_url", "image_url"]
    assert blocks[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


def test_vision_enforces_count_cap_with_note():
    # Hand-rolled oversize envelope (the helper rejects it eagerly; the renderer must too).
    env = {"text": "cap me", "images": [{"b64": PNG_B64, "mime": "image/png"}] * (MAX_IMAGES_PER_RESULT + 2)}
    blocks = render_multimodal_content(env, vision=True)
    assert len(blocks) == 1 + MAX_IMAGES_PER_RESULT
    assert f"max {MAX_IMAGES_PER_RESULT}" in blocks[0]["text"]


def test_vision_drops_oversized_image_with_note():
    big = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()
    env = {"text": "t", "images": [{"b64": big, "mime": "image/png"}, {"b64": PNG_B64, "mime": "image/png"}]}
    blocks = render_multimodal_content(env, vision=True)
    assert len(blocks) == 2  # text + the one surviving image
    assert "exceeds" in blocks[0]["text"]
    # The oversized payload is gone from the content entirely.
    assert big not in json.dumps(blocks)


def test_vision_with_no_surviving_images_degrades_to_text():
    env = {"text": "t", "images": [{"mime": "image/png"}]}  # no b64/path
    out = render_multimodal_content(env, vision=True)
    assert isinstance(out, str) and "dropped" in out


# ── rendering: text-only degradation ──────────────────────────────────────────


def test_text_only_degrades_to_caption_plus_note():
    env = parse_multimodal_result(_envelope("the caption"))
    out = render_multimodal_content(env, vision=False)
    assert isinstance(out, str)
    assert out.startswith("the caption")
    assert "text-only" in out
    assert PNG_B64 not in out  # never leak base64 into a text model's context


def test_text_only_uses_describe_fallback_when_available():
    seen = {}

    def describe(raw, mime, filename):
        seen["args"] = (raw, mime, filename)
        return "a red square on white"

    env = parse_multimodal_result(_envelope("the caption"))
    out = render_multimodal_content(env, vision=False, describe_fn=describe)
    assert "a red square on white" in out
    assert seen["args"][0] == PNG_BYTES and seen["args"][1] == "image/png"


def test_text_only_survives_a_raising_describe_fn():
    def describe(raw, mime, filename):
        raise RuntimeError("describe model down")

    env = parse_multimodal_result(_envelope("still fine"))
    out = render_multimodal_content(env, vision=False, describe_fn=describe)
    assert out.startswith("still fine") and "text-only" in out


# ── middleware: the hot path must be provably unchanged ───────────────────────


def test_plain_string_tool_result_passes_through_as_the_same_object():
    mw = MultimodalToolResultMiddleware(vision=True)
    original = ToolMessage(content="just text", tool_call_id="c1")
    assert mw.wrap_tool_call(_req(), lambda r: original) is original


@pytest.mark.asyncio
async def test_plain_string_passes_through_async_too():
    mw = MultimodalToolResultMiddleware(vision=True)
    original = ToolMessage(content="just text", tool_call_id="c1")

    async def handler(r):
        return original

    assert await mw.awrap_tool_call(_req(), handler) is original


def test_non_toolmessage_results_pass_through():
    mw = MultimodalToolResultMiddleware(vision=True)
    sentinel_free = {"some": "command-like value"}
    assert mw.wrap_tool_call(_req(), lambda r: sentinel_free) is sentinel_free


def test_list_content_toolmessage_passes_through():
    mw = MultimodalToolResultMiddleware(vision=True)
    original = ToolMessage(content=[{"type": "text", "text": "already blocks"}], tool_call_id="c1")
    assert mw.wrap_tool_call(_req(), lambda r: original) is original


# ── middleware: the opt-in rewrite ────────────────────────────────────────────


def test_envelope_becomes_blocks_on_vision_model():
    mw = MultimodalToolResultMiddleware(vision=True)
    msg = ToolMessage(content=_envelope("generated"), tool_call_id="c9", status="success")
    out = mw.wrap_tool_call(_req(call_id="c9"), lambda r: msg)
    assert isinstance(out, ToolMessage)
    assert out.tool_call_id == "c9" and out.status == "success"
    assert isinstance(out.content, list)
    assert out.content[0]["type"] == "text" and out.content[1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_envelope_becomes_blocks_on_vision_model_async():
    mw = MultimodalToolResultMiddleware(vision=True)

    async def handler(r):
        return ToolMessage(content=_envelope("generated"), tool_call_id="c9")

    out = await mw.awrap_tool_call(_req(call_id="c9"), handler)
    assert isinstance(out.content, list) and out.content[1]["type"] == "image_url"


def test_envelope_degrades_to_text_on_text_only_model():
    mw = MultimodalToolResultMiddleware(vision=False)
    msg = ToolMessage(content=_envelope("generated"), tool_call_id="c9")
    out = mw.wrap_tool_call(_req(call_id="c9"), lambda r: msg)
    assert isinstance(out.content, str)
    assert out.content.startswith("generated") and PNG_B64 not in out.content


def test_malformed_envelope_never_leaves_the_blob_in_context():
    mw = MultimodalToolResultMiddleware(vision=True)
    msg = ToolMessage(content=_SENTINEL + "{broken", tool_call_id="c9")
    out = mw.wrap_tool_call(_req(call_id="c9"), lambda r: msg)
    assert isinstance(out.content, str) and "malformed" in out.content


# ── wiring ────────────────────────────────────────────────────────────────────


def test_build_from_config_reads_model_vision_flag():
    vision_cfg = SimpleNamespace(model_vision=True, image_describe_model="")
    mw = build_multimodal_middleware(vision_cfg)
    assert mw._vision is True and mw._describe_fn is None

    text_cfg = SimpleNamespace(model_vision=False, image_describe_model="")
    mw = build_multimodal_middleware(text_cfg)
    assert mw._vision is False and mw._describe_fn is None  # no describe model configured


def test_build_vision_override_for_subagent_models():
    cfg = SimpleNamespace(model_vision=True, image_describe_model="")
    mw = build_multimodal_middleware(cfg, vision=False)  # aux-model delegation
    assert mw._vision is False


def test_lead_middleware_chain_includes_multimodal_rewrite():
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    chain = _build_middleware(LangGraphConfig(api_key="k"), None)
    assert any(isinstance(m, MultimodalToolResultMiddleware) for m in chain)
