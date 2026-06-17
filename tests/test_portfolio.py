"""portfolio plugin tests (ADR 0055 P1).

The PM orchestration tools are pure composition over fleet × delegates ×
project_board — so the tests stub all three: no fleet, no A2A, no HTTP. They
assert the tools address the right remote board, dispatch over A2A with the
stored bearer, and read the structured board back.
"""

from __future__ import annotations

import json

import pytest

from plugins import portfolio


def _tool(name: str):
    return next(t for t in portfolio._tools() if t.name == name)


# ── portfolio_boards ─────────────────────────────────────────────────────────


def test_boards_lists_only_remote_members(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: [
            {"name": "host", "host": True},
            {"name": "team-web", "remote": True, "url": "https://web.example", "running": True},
            {"name": "team-api", "remote": True, "url": "https://api.example", "running": False},
            {"name": "local-x", "port": 7890},  # a local member, not a remote → excluded
        ],
    )
    out = json.loads(_tool("portfolio_boards").invoke({}))
    assert {b["board"] for b in out} == {"team-web", "team-api"}
    assert next(b for b in out if b["board"] == "team-web")["reachable"] is True
    assert next(b for b in out if b["board"] == "team-api")["reachable"] is False


def test_boards_empty_message(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "status", lambda: [{"name": "host", "host": True}])
    assert "No team boards yet" in _tool("portfolio_boards").invoke({})


# ── portfolio_dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_sends_over_a2a_with_the_stored_bearer(monkeypatch):
    from graph.fleet import supervisor
    from plugins.delegates import adapters

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example/", "token": "tok-web"}],
    )
    captured = {}

    async def fake_dispatch(d, query, *, timeout=None):
        captured.update(url=d.url, token=d.auth_token, scheme=d.auth_scheme, query=query, timeout=timeout)
        return "Created bd-7; state ready."

    monkeypatch.setattr(adapters.ADAPTERS["a2a"], "dispatch", fake_dispatch)

    out = await _tool("portfolio_dispatch").ainvoke(
        {
            "board": "team-web",
            "title": "Add /healthz",
            "spec": "expose a readiness probe",
            "acceptance_criteria": "returns 200 when ready",
            "files_to_modify": "server.py",
        }
    )
    assert out == "Created bd-7; state ready."
    assert captured["url"] == "https://web.example/a2a"  # /a2a appended, trailing slash normalized
    assert captured["token"] == "tok-web" and captured["scheme"] == "bearer"
    # the instruction carries the spec + tells the team lead to use its board tools
    assert "Add /healthz" in captured["query"] and "board_create_feature" in captured["query"]
    assert "returns 200 when ready" in captured["query"] and "server.py" in captured["query"]


@pytest.mark.asyncio
async def test_dispatch_unknown_board(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    out = await _tool("portfolio_dispatch").ainvoke({"board": "nope", "title": "t", "spec": "s"})
    assert "no team board named 'nope'" in out


# ── portfolio_board_read ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_board_read_fetches_structured_features_with_bearer(monkeypatch):
    import httpx

    from graph.fleet import supervisor
    from security import policy

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [{"id": "r1", "name": "team-web", "url": "https://web.example", "token": "tok-web"}],
    )
    monkeypatch.setattr(policy, "check_url", lambda _url: "")  # allow the read URL

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"features": [{"id": "bd-1", "title": "T", "state": "ready"}]}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, headers=None, params=None):
            seen.update(url=url, headers=headers, params=params)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    out = json.loads(await _tool("portfolio_board_read").ainvoke({"board": "team-web", "state": "ready"}))
    assert out == [{"id": "bd-1", "title": "T", "state": "ready"}]
    assert seen["url"] == "https://web.example/api/plugins/project_board/features"
    assert seen["headers"]["Authorization"] == "Bearer tok-web"
    assert seen["params"] == {"state": "ready"}


@pytest.mark.asyncio
async def test_board_read_unknown_board(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    out = await _tool("portfolio_board_read").ainvoke({"board": "nope"})
    assert "no team board named 'nope'" in out


# ── manifest / loader ────────────────────────────────────────────────────────


def test_manifest_discovers_and_ships_disabled():
    from pathlib import Path

    from graph.plugins.loader import discover_plugins

    plugins_root = Path(__file__).resolve().parent.parent / "plugins"
    by_id = {m.id: m for m in discover_plugins([plugins_root])}
    m = by_id.get("portfolio")
    assert m is not None, "portfolio plugin not discovered"
    assert m.version and m.enabled is False  # enable is the operator's trust decision


def test_register_exposes_the_three_tools():
    seen = []

    class _Reg:
        def register_tool(self, t):
            seen.append(t.name)

    portfolio.register(_Reg())
    assert set(seen) == {"portfolio_boards", "portfolio_dispatch", "portfolio_board_read"}
