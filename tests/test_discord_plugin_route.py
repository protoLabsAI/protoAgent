"""The discord plugin's Test-connection route must read the request body.

Regression for a PEP 563 footgun: the plugin module uses
`from __future__ import annotations`, so a *function-local* Pydantic body model
can't be resolved by FastAPI's `get_type_hints()` (module-globals only) — the
body is silently ignored and every token reads as empty, breaking "Test
connection" even for a valid token. The model must stay MODULE-LEVEL.
"""

from __future__ import annotations

import importlib.util
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_discord_plugin():
    from graph.config_io import _BUNDLE_CONFIG_DIR

    path = _BUNDLE_CONFIG_DIR.parent / "plugins" / "discord" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "discord_plugin_under_test", str(path),
        submodule_search_locations=[str(path.parent)],
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_test_discord_route_reads_the_token_from_the_body(monkeypatch):
    m = _load_discord_plugin()

    # Echo the token validate_token actually receives.
    import surfaces.discord as sd

    async def _echo(token):
        return (False, None, f"RECEIVED={token!r}")

    monkeypatch.setattr(sd, "validate_token", _echo)

    reg = types.SimpleNamespace(config={}, host=types.SimpleNamespace(config=lambda: None))
    app = FastAPI()
    app.include_router(m._build_router(reg))
    client = TestClient(app)

    r = client.post("/api/config/test-discord", json={"bot_token": "TOK123"})
    assert r.status_code == 200
    # The bug symptom: a non-empty token arriving as '' (body ignored).
    assert "TOK123" in r.json()["error"], "route did not read bot_token from the body"

    r2 = client.post("/api/config/test-discord", json={"bot_token": ""})
    assert "''" in r2.json()["error"]  # empty still handled
