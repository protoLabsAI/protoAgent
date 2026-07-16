"""gzip on the server (issue #2005) — and the load-bearing invariant that it MUST NOT
touch SSE.

The server compresses text/JSON responses, but the chat stream, the event bus, A2A
`message/stream`, and the fleet proxy all deliver `text/event-stream`. Gzipping a live
SSE response buffers events until the compressor emits a block — a far worse regression
than the bytes saved. We rely on Starlette's GZipMiddleware excluding `text/event-stream`
by default; these tests pin that contract so a Starlette bump that drops the exclusion
turns this red instead of silently stalling the chat stream in prod.
"""

from __future__ import annotations

import gzip

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

# Mirror the server's registration exactly (server/__init__.py) — if those params drift,
# update them here so the test keeps describing the real config.
_MIN_SIZE = 1024
_COMPRESSLEVEL = 6


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=_MIN_SIZE, compresslevel=_COMPRESSLEVEL)

    @app.get("/big.json")
    async def _big():
        # Comfortably over minimum_size and highly compressible.
        return {"blob": "protoagent " * 400}

    @app.get("/tiny.json")
    async def _tiny():
        return {"ok": True}

    @app.get("/stream")
    async def _stream():
        async def _gen():
            for i in range(50):
                yield f"data: event-{i}\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return app


def test_large_json_is_gzipped_when_requested() -> None:
    c = TestClient(_app())
    r = c.get("/big.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in r.headers.get("vary", "").lower()  # cache-correctness


def test_no_compression_without_accept_encoding() -> None:
    # httpx defaults to advertising gzip; force it off to prove the server honours it.
    c = TestClient(_app())
    r = c.get("/big.json", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_tiny_json_is_left_uncompressed() -> None:
    c = TestClient(_app())
    r = c.get("/tiny.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers  # below minimum_size — gzip framing isn't worth it


def test_sse_is_never_gzipped_even_when_gzip_is_offered() -> None:
    """The invariant that protects the chat stream: a text/event-stream response is
    passed through with NO content-encoding, so events aren't held back by the gzip
    block buffer."""
    c = TestClient(_app())
    r = c.get("/stream", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in r.headers
    # Body is the raw SSE frames, not a gzip stream.
    assert "data: event-0" in r.text
    assert "data: event-49" in r.text


def test_already_encoded_body_is_not_double_compressed() -> None:
    """The fleet proxy forwards an upstream body verbatim with its own content-encoding
    (it streams raw bytes). The middleware must not gzip an already-encoded body."""
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=_MIN_SIZE, compresslevel=_COMPRESSLEVEL)
    precompressed = gzip.compress(b"upstream " * 400)

    @app.get("/proxied")
    async def _proxied():
        from fastapi import Response

        return Response(
            content=precompressed,
            media_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    r = TestClient(app).get("/proxied", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    # httpx auto-decodes the single Content-Encoding: gzip layer the endpoint set. If the
    # middleware had ALSO gzipped the body, a second layer would survive here and the
    # bytes wouldn't match — so an exact match proves it passed the pre-encoded body
    # through untouched rather than double-compressing it.
    assert r.content == b"upstream " * 400
