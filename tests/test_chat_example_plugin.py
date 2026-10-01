"""chat_example plugin (ADR 0045) — the reference chat-slot panel."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graph.plugins.manifest import load_manifest

# Lives in examples/ (NOT plugins/) on purpose: it's a copy-me reference, not a
# shipped plugin — the loader never discovers it unless a user copies it in.
_PLUGIN_DIR = Path("examples/plugins/chat_example")


def _load_module():
    spec = importlib.util.spec_from_file_location("chat_example_under_test", _PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_manifest_claims_the_chat_slot() -> None:
    """The view declares slot:"chat" and it survives manifest parsing — the whole
    backend contract of ADR 0045 is that unknown view keys pass through intact."""
    m = load_manifest(_PLUGIN_DIR)
    assert m is not None and m.id == "chat_example"
    assert m.enabled is False  # opt-in (lean core)
    assert len(m.views) == 1
    view = m.views[0]
    assert view["slot"] == "chat"
    assert view["path"] == "/plugins/chat_example/panel"


def test_panel_route_serves_the_contract_page() -> None:
    """register() mounts a router whose /panel page carries the handshake +
    slug-aware base + non-streaming chat call — the example's teaching points."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mod = _load_module()

    routers: list[tuple] = []

    class _Reg:
        def register_router(self, router, prefix):
            routers.append((router, prefix))

    mod.register(_Reg())
    assert len(routers) == 1
    router, prefix = routers[0]
    # Served OUTSIDE /api/ — the iframe page load can't carry a bearer header.
    assert prefix == "/plugins/chat_example"

    app = FastAPI()
    app.include_router(router, prefix=prefix)
    page = TestClient(app).get("/plugins/chat_example/panel")
    assert page.status_code == 200
    body = page.text
    assert "protoagent:init" in body  # bearer + theme handshake (ADR 0038)
    assert 'split("/plugins/")' in body  # slug-aware base (ADR 0042)
    assert "/api/chat" in body  # the documented non-streaming turn


def _panel_error_detail_fn() -> str:
    import re

    text = (_PLUGIN_DIR / "panel.html").read_text(encoding="utf-8")
    m = re.search(r"^  function errorDetail\(body, fallback\) \{\n.*?^  \}\n", text, re.S | re.M)
    assert m, "panel.html must keep its errorDetail(body, fallback) helper"
    return m.group(0)


@pytest.mark.platform_sensitive  # runs node in a subprocess
def test_panel_reads_the_message_of_an_object_detail() -> None:
    """`POST /api/chat` answers a failed turn with an object detail (#3973); the panel
    authors copy must show its message, not "Turn failed: [object Object]"."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = [
        (
            {"detail": {"code": "server_error", "message": "The model provider closed the stream."}},
            "The model provider closed the stream.",
        ),
        ({"detail": "tasks not enabled"}, "tasks not enabled"),
        ({"detail": [{"msg": "Field required"}]}, "Field required"),
        ({"detail": {"code": "x"}}, "500 Internal Server Error"),
        ({}, "500 Internal Server Error"),
    ]
    script = _panel_error_detail_fn() + (
        "const cases = " + json.dumps([c[0] for c in cases]) + ";\n"
        "process.stdout.write(JSON.stringify(cases.map((b) => errorDetail(b, '500 Internal Server Error'))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out) == [c[1] for c in cases]
