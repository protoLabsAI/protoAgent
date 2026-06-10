"""chat_example plugin (ADR 0045) — the reference chat-slot panel."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from graph.plugins.manifest import load_manifest

# Lives in examples/ (NOT plugins/) on purpose: it's a copy-me reference, not a
# shipped plugin — the loader never discovers it unless a user copies it in.
_PLUGIN_DIR = Path("examples/plugins/chat_example")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "chat_example_under_test", _PLUGIN_DIR / "__init__.py"
    )
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
    assert "protoagent:init" in body          # bearer + theme handshake (ADR 0038)
    assert 'split("/plugins/")' in body       # slug-aware base (ADR 0042)
    assert "/api/chat" in body                # the documented non-streaming turn
