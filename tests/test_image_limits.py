"""Images stay inside the provider's limits — the session-poisoning fix.

Live failure (v0.189.0, anthropic-oauth claude-opus-5-5): a plugin tool returned a
2560×1600 screenshot through ``multimodal_tool_result``. Once the conversation held
more than 20 images, Anthropic answered every request with HTTP 400 ("At least one of
the image dimensions exceed max allowed size for many-image requests: 2000 pixels"),
and because the image sat in the CHECKPOINTED history every later turn failed too.

Two layers are pinned here: the source (tool results / attachments are downscaled
before they are stored) and the request boundary (every outgoing payload is clamped
without touching the stored history, which is what unpoisons an existing session).
"""

from __future__ import annotations

import base64
import copy
import struct
import zlib
from io import BytesIO

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph import image_limits
from graph.config import LangGraphConfig

PIL = pytest.importorskip("PIL", reason="Pillow drives the downscale path (installed in CI)")
from PIL import Image  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────


def _png(w: int = 2560, h: int = 1600, *, alpha: bool = False) -> bytes:
    im = Image.new("RGBA" if alpha else "RGB", (w, h), (30, 120, 200, 128) if alpha else (30, 120, 200))
    # A little detail so the encoders do real work.
    for x in range(0, w, 97):
        im.putpixel((x, x % h), (255, 255, 255, 255) if alpha else (255, 255, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _bare_png_header(w: int, h: int) -> bytes:
    """A syntactically valid PNG of the given size with NO Pillow involved — enough for
    the header sniffer (the pixels are a 1-row stub; nothing decodes them)."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 4)) + chunk(b"IEND", b"")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{_b64(raw)}"


def _dims_b64(data: str) -> tuple[int, int]:
    return Image.open(BytesIO(base64.b64decode(data))).size


@pytest.fixture(autouse=True)
def _default_limits():
    image_limits.configure(
        max_side=image_limits.DEFAULT_MAX_SIDE,
        max_images_per_request=image_limits.DEFAULT_MAX_IMAGES_PER_REQUEST,
    )
    image_limits._fit_cache.clear()
    yield
    image_limits.configure(
        max_side=image_limits.DEFAULT_MAX_SIDE,
        max_images_per_request=image_limits.DEFAULT_MAX_IMAGES_PER_REQUEST,
    )
    image_limits._fit_cache.clear()


@pytest.fixture(scope="module")
def big_png() -> bytes:
    return _png()


def _poisoned_history(big: bytes, n: int) -> list:
    """A history shaped like the live failure: ``n`` tool turns, each a ToolMessage
    carrying a 2560×1600 screenshot (the shape the multimodal middleware stores)."""
    msgs: list = [HumanMessage("look at the campaign")]
    for i in range(n):
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"t{i}", "name": "campaign_view", "args": {}}]))
        msgs.append(
            ToolMessage(
                tool_call_id=f"t{i}",
                content=[
                    {"type": "text", "text": f"campaign screenshot {i}"},
                    {"type": "image_url", "image_url": {"url": _uri(big)}},
                ],
            )
        )
    msgs.append(HumanMessage("and now?"))
    return msgs


def _anthropic_images(payload: dict) -> list[dict]:
    out = []
    for m in payload["messages"]:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "image":
                out.append(b)
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                out += [c for c in b["content"] if c.get("type") == "image"]
    return out


def _anthropic_texts(payload: dict) -> list[str]:
    out = []
    for m in payload["messages"]:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "text":
                out.append(b["text"])
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                out += [c["text"] for c in b["content"] if c.get("type") == "text"]
    return out


@pytest.fixture
def oauth_llm(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-WIRE")
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic not installed")
    ao._reset_token_cache()
    from graph.llm import create_llm

    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5-5"))
    llm.max_retries = 0
    yield llm
    ao._reset_token_cache()


# ── header sniffing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_dimensions_read_from_the_header(fmt):
    im = Image.new("RGB", (1234, 567), (10, 20, 30))
    buf = BytesIO()
    im.save(buf, format=fmt)
    assert image_limits.image_dimensions(buf.getvalue()) == (1234, 567)


def test_dimensions_need_no_pillow():
    assert image_limits.image_dimensions(_bare_png_header(4000, 3000)) == (4000, 3000)
    assert image_limits.image_dimensions(b"not an image") is None


# ── layer 1: the source ─────────────────────────────────────────────────────


def test_fit_image_downscales_to_the_recommended_size_keeping_aspect(big_png):
    out, mime = image_limits.fit_image(big_png, "image/png")
    w, h = Image.open(BytesIO(out)).size
    assert max(w, h) == 1568
    assert abs(w / h - 2560 / 1600) < 0.01
    assert mime == "image/jpeg"  # opaque → JPEG
    assert len(out) < len(big_png)


def test_fit_image_keeps_transparency_lossless():
    out, mime = image_limits.fit_image(_png(2400, 1200, alpha=True), "image/png")
    im = Image.open(BytesIO(out))
    assert max(im.size) == 1568 and mime == "image/png" and im.mode == "RGBA"


def test_fit_image_leaves_a_small_image_untouched():
    small = _png(800, 600)
    out, mime = image_limits.fit_image(small, "image/png")
    assert out is small and mime == "image/png"


def test_configured_max_side_is_honoured_and_capped_at_2000(big_png):
    image_limits.configure(max_side=1000)
    out, _ = image_limits.fit_image(big_png, "image/png")
    assert max(Image.open(BytesIO(out)).size) == 1000
    image_limits.configure(max_side=5000)  # no setting may store a many-image-unsafe image
    assert image_limits.max_side() == 2000


def test_create_llm_pushes_the_config_limits():
    from graph.llm import create_llm

    try:
        create_llm(LangGraphConfig(image_max_side=900, max_images_per_request=7, api_key="sk-x", api_base="http://gw.test/v1"))
    except Exception:  # noqa: BLE001 — only the configure side effect matters here
        pass
    assert image_limits.max_side() == 900
    assert image_limits.max_images_per_request() == 7


def test_multimodal_tool_result_downscales_the_live_failure_screenshot(big_png):
    from graph.multimodal import multimodal_tool_result, parse_multimodal_result, render_multimodal_content

    env = parse_multimodal_result(multimodal_tool_result("campaign", images=[{"b64": _b64(big_png), "mime": "image/png"}]))
    (img,) = env["images"]
    assert max(_dims_b64(img["b64"])) <= 1568
    assert img["mime"] == "image/jpeg"
    blocks = render_multimodal_content(env, vision=True)
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert max(_dims_b64(url.split(",", 1)[1])) <= 1568


def test_render_fits_an_envelope_that_skipped_the_helper(big_png, tmp_path):
    """A hand-built envelope (path image) is fitted on render, too."""
    from graph.multimodal import render_multimodal_content

    p = tmp_path / "shot.png"
    p.write_bytes(big_png)
    blocks = render_multimodal_content({"text": "x", "images": [{"path": str(p), "mime": "image/png"}]}, vision=True)
    assert max(_dims_b64(blocks[1]["image_url"]["url"].split(",", 1)[1])) <= 1568


def test_user_attachment_is_fitted_before_it_enters_history(big_png, monkeypatch):
    import importlib

    from runtime.state import STATE

    chat = importlib.import_module("server.chat")  # `server.chat` the attribute is a function

    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(model_vision=True), raising=False)
    msg = chat._vision_human_message("see", [("image/png", _uri(big_png))], incognito=True)
    url = next(b for b in msg.content if b.get("type") == "image_url")["image_url"]["url"]
    assert max(_dims_b64(url.split(",", 1)[1])) <= 1568
    # http URLs are not ours to fetch — passed through untouched.
    assert image_limits.fit_data_uri("https://example.test/a.png") == "https://example.test/a.png"


# ── layer 2: the request boundary ───────────────────────────────────────────


def test_anthropic_payload_leaves_a_legal_image_alone(oauth_llm):
    """One 2560×1600 image in a ≤20-image request is legal (≤ 8000 px): the boundary only
    re-encodes what the provider would reject, so it is sent as stored."""
    big = _png()
    payload = oauth_llm._get_request_payload(_poisoned_history(big, 1))
    (img,) = _anthropic_images(payload)
    assert _dims_b64(img["source"]["data"]) == (2560, 1600)


def test_anthropic_payload_with_25_large_images_is_unpoisoned(oauth_llm, big_png):
    """The live failure: 25 screenshots of 2560×1600 in the checkpointed history. With
    the default cap the oldest 5 become notes; with no cap all 25 are downscaled under
    the 2000 px many-image limit. Either way, the stored messages are not mutated."""
    history = _poisoned_history(big_png, 25)
    snapshot = copy.deepcopy(history)

    payload = oauth_llm._get_request_payload(history)
    imgs = _anthropic_images(payload)
    assert len(imgs) == 20
    for img in imgs:
        assert max(_dims_b64(img["source"]["data"])) <= 8000
    omitted = [t for t in _anthropic_texts(payload) if t.startswith("[image omitted:")]
    assert omitted == [f"[image omitted: campaign screenshot {i}]" for i in range(5)]
    assert history == snapshot

    image_limits.configure(max_images_per_request=0)  # no count cap → many-image rule
    payload = oauth_llm._get_request_payload(history)
    imgs = _anthropic_images(payload)
    assert len(imgs) == 25
    for img in imgs:
        w, h = _dims_b64(img["source"]["data"])
        assert max(w, h) <= image_limits.MANY_IMAGES_MAX_SIDE
        assert abs(w / h - 1.6) < 0.01
    assert history == snapshot


def test_non_image_content_is_untouched(oauth_llm, big_png):
    history = _poisoned_history(big_png, 22)
    payload = oauth_llm._get_request_payload(history)
    # Every tool_use / tool_result id and every caption survives in order.
    tool_results = [b for m in payload["messages"] for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_result"]
    assert [b["tool_use_id"] for b in tool_results] == [f"t{i}" for i in range(22)]
    texts = _anthropic_texts(payload)
    for i in range(22):
        assert f"campaign screenshot {i}" in texts
    assert payload["system"][0]["text"].startswith("You are Claude Code")


def test_payload_without_images_is_returned_as_is():
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    assert image_limits.clamp_request_images(payload, wire="anthropic") is payload
    assert image_limits.clamp_request_images(payload, wire="openai") is payload


def test_without_pillow_an_illegal_image_becomes_a_note(monkeypatch):
    """Lean install: nothing can downscale, but the request must still go through."""
    monkeypatch.setattr(image_limits, "_pil", lambda: None)
    huge = _b64(_bare_png_header(2560, 1600))
    blocks = [
        {"type": "text", "text": f"shot {i}"} if j == 0 else {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": huge}}
        for i in range(25)
        for j in range(2)
    ]
    payload = {"messages": [{"role": "user", "content": blocks}]}
    image_limits.configure(max_images_per_request=0)
    out = image_limits.clamp_request_images(payload, wire="anthropic")
    assert _anthropic_images(out) == []  # every one breaks the 2000 px rule
    assert sum(t.startswith("[image omitted:") for t in _anthropic_texts(out)) == 25
    assert payload["messages"][0]["content"] is blocks and len(blocks) == 50  # input untouched
    # …and fit_image keeps the original (the boundary is the safety net).
    raw = _bare_png_header(2560, 1600)
    assert image_limits.fit_image(raw, "image/png")[0] is raw


def test_gateway_payload_is_clamped_too(big_png):
    from graph.llm import _ReasoningChatOpenAI

    model = _ReasoningChatOpenAI(model="protolabs/smart", api_key="sk-test", base_url="http://gw.test/v1")
    history = _poisoned_history(big_png, 23)
    snapshot = copy.deepcopy(history)
    image_limits.configure(max_images_per_request=0)
    payload = model._get_request_payload(history)
    urls = [b["image_url"]["url"] for m in payload["messages"] if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "image_url"]
    assert len(urls) == 23
    assert all(max(_dims_b64(u.split(",", 1)[1])) <= 2000 for u in urls)
    assert history == snapshot


def test_request_budget_omits_the_oldest_first(monkeypatch):
    monkeypatch.setattr(image_limits, "REQUEST_IMAGE_B64_BUDGET", 3 * 1000)
    small = _b64(_bare_png_header(100, 100))
    pad = small + "A" * (900 - len(small))  # ~900 b64 chars each, header still parses
    content = []
    for i in range(5):
        content += [{"type": "text", "text": f"pic {i}"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": pad}}]
    out = image_limits.clamp_request_images({"messages": [{"role": "user", "content": content}]}, wire="anthropic")
    assert len(_anthropic_images(out)) == 3
    assert out["messages"][0]["content"][1]["type"] == "text"  # the oldest went first


@pytest.mark.asyncio
async def test_wire_body_carries_only_legal_images(oauth_llm, big_png):
    """End to end at the httpx boundary: the body Anthropic would receive."""
    from tests.test_oauth_identity_every_call import _Capture, _wire

    cap = _Capture()
    _wire(oauth_llm, cap)
    image_limits.configure(max_images_per_request=0)
    await oauth_llm.ainvoke(_poisoned_history(big_png, 21))
    (body,) = cap.bodies
    imgs = _anthropic_images(body)
    assert len(imgs) == 21
    assert all(max(_dims_b64(i["source"]["data"])) <= 2000 for i in imgs)
