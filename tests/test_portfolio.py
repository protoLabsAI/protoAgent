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


def test_register_exposes_the_tools():
    seen = []

    class _Reg:
        def register_tool(self, t):
            seen.append(t.name)

    portfolio.register(_Reg())
    assert set(seen) == {"portfolio_boards", "portfolio_dispatch", "portfolio_board_read", "portfolio_rollup"}


# ── portfolio_rollup (P2) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollup_projects_bounded_counts_and_only_blocked_critical(monkeypatch):
    from graph.fleet import supervisor
    from plugins import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "team-web", "url": "https://web.example", "token": "t"},
            {"id": "r2", "name": "team-api", "url": "https://api.example", "token": "t"},
        ],
    )

    boards = {
        "team-web": [
            {"id": "w1", "title": "ready feat", "board_state": "ready"},
            {"id": "w2", "title": "blocked feat", "board_state": "in_progress", "blocked": True},
            {"id": "w3", "title": "foundation", "board_state": "in_progress", "foundation": True},
            {"id": "w4", "title": "done foundation", "board_state": "done", "foundation": True},
        ],
        "team-api": [{"id": "a1", "title": "x", "board_state": "backlog"}],
    }

    async def fake_fetch(rec, state=""):
        return boards[rec["name"]]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)

    out = {r["board"]: r for r in json.loads(await _tool("portfolio_rollup").ainvoke({}))}
    web = out["team-web"]
    assert web["total"] == 4
    assert web["counts"] == {"ready": 1, "in_progress": 2, "done": 1}
    assert web["blocked"] == [{"id": "w2", "title": "blocked feat"}]  # only the blocked one
    assert web["critical_path"] == [{"id": "w3", "title": "foundation", "state": "in_progress"}]  # done foundation excluded
    # the rollup is BOUNDED — it carries counts + blocked/critical only, never the full feature list
    assert "spec" not in json.dumps(web) and "files_to_modify" not in json.dumps(web)
    assert out["team-api"]["counts"] == {"backlog": 1}


@pytest.mark.asyncio
async def test_rollup_filters_by_boards_arg(monkeypatch):
    from graph.fleet import supervisor
    from plugins import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "team-web", "url": "https://web.example", "token": "t"},
            {"id": "r2", "name": "team-api", "url": "https://api.example", "token": "t"},
        ],
    )

    async def fake_fetch(rec, state=""):
        return []

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    out = json.loads(await _tool("portfolio_rollup").ainvoke({"boards": "team-api"}))
    assert [r["board"] for r in out] == ["team-api"]


@pytest.mark.asyncio
async def test_rollup_tolerates_an_unreachable_board(monkeypatch):
    from graph.fleet import supervisor
    from plugins import portfolio as pf

    monkeypatch.setattr(
        supervisor,
        "list_remotes",
        lambda: [
            {"id": "r1", "name": "up", "url": "https://up.example", "token": "t"},
            {"id": "r2", "name": "down", "url": "https://down.example", "token": "t"},
        ],
    )

    async def fake_fetch(rec, state=""):
        if rec["name"] == "down":
            raise pf._BoardUnavailable("no project board exposed (project_board not enabled there)")
        return [{"id": "x", "board_state": "ready"}]

    monkeypatch.setattr(pf, "_fetch_board_features", fake_fetch)
    out = {r["board"]: r for r in json.loads(await _tool("portfolio_rollup").ainvoke({}))}
    assert out["up"]["counts"] == {"ready": 1}
    assert "error" in out["down"] and "no project board" in out["down"]["error"]  # one bad board doesn't sink the rollup


@pytest.mark.asyncio
async def test_rollup_no_boards_message(monkeypatch):
    from graph.fleet import supervisor

    monkeypatch.setattr(supervisor, "list_remotes", lambda: [])
    assert "No team boards yet" in await _tool("portfolio_rollup").ainvoke({})
