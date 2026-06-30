"""Single-agent plugin LIFECYCLE smoke (ADR 0027 / 0019) — the "lock it in" test.

Drives one host agent through the whole loop a user does — install → enable → configure
(a secret) → use → update → uninstall — through the real layers (installer, loader,
config/secrets resolution), tying together what the per-layer tests check in isolation.
This is the regression net for the class of bugs that shipped this cycle (a secret saved
but read back "unset"; install-but-never-loaded).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph.plugins import installer


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_rich_plugin_repo(root: Path, pid: str = "demo_kit") -> Path:
    """A plugin that exercises every contribution the lifecycle touches: a tool, a
    declared secret + Settings field, and a console view."""
    repo = root / f"src-{pid}"
    repo.mkdir(parents=True)
    (repo / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: Demo Kit\nversion: 0.1.0\ndescription: lifecycle test plugin\n"
        f"config_section: {pid}\n"
        "secrets: [api_key]\n"
        "settings:\n  - {{ key: api_key, label: Key, type: secret }}\n".format()
        + f"views:\n  - {{ id: main, label: Demo, icon: Boxes, path: /api/plugins/{pid}/view }}\n"
    )
    (repo / "__init__.py").write_text(
        "from langchain_core.tools import tool\n"
        "def register(registry):\n"
        "    @tool\n"
        "    def demo_kit_hello(name: str = 'world') -> str:\n"
        '        """say hi"""\n'
        "        return f'hello {name}'\n"
        "    registry.register_tool(demo_kit_hello)\n"
        "    from fastapi import APIRouter\n"
        "    from fastapi.responses import HTMLResponse\n"
        "    r = APIRouter()\n"
        "    @r.get('/view')\n"
        "    async def _v():\n"
        "        return HTMLResponse('<h1>demo</h1>')\n"
        "    registry.register_router(r, prefix='/api/plugins/demo_kit')\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """One host agent's data area — install dir, lock, config + secrets — all temp."""
    import graph.config_io as cio

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    # The REAL single-agent layout: the install dir is the instance plugins root
    # (PROTOAGENT_PLUGINS_DIR), the same dir _resolve_plugin_config roots at
    # (instance_paths().plugins_dir). Keeping these aligned is what the double-scope
    # bug broke — model it faithfully.
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(cfg / "plugins"))
    # save_secrets / uninstall-purge resolve config + secrets via these accessors;
    # point them at the temp config dir so the round-trip stays in the sandbox.
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: cfg / "secrets.yaml")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: cfg / "langgraph-config.yaml")
    return tmp_path


def test_plugin_lifecycle_single_agent(agent, monkeypatch):
    from graph.config import LangGraphConfig
    from graph.config_io import save_secrets
    from graph.plugins import loader as plugin_loader
    from graph.plugins.loader import load_plugins

    cfg_path = agent / "cfg" / "langgraph-config.yaml"

    # ── 1. INSTALL (from a local git repo — fetch ≠ enable) ──────────────────────
    repo = _make_rich_plugin_repo(agent)
    summary = installer.install(str(repo))
    assert summary["id"] == "demo_kit"
    assert installer.list_installed()[0]["id"] == "demo_kit"
    installed_dir = installer.live_plugins_dir() / "demo_kit"
    assert (installed_dir / "protoagent.plugin.yaml").exists()

    # ── 2. ENABLE → the loader finds + registers it (tool + view router) ─────────
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [installer.live_plugins_dir()])
    cfg_path.write_text("plugins:\n  enabled: [demo_kit]\n")
    res = load_plugins(LangGraphConfig.from_yaml(str(cfg_path)))
    meta = next(m for m in res.meta if m["id"] == "demo_kit")
    assert meta["loaded"], meta.get("error")
    assert "demo_kit_hello" in meta["tools"]
    assert any(r.get("plugin_id") == "demo_kit" for r in res.routers)  # the view mounted

    # ── 3. CONFIGURE a secret → it resolves back into plugin_config (is_set) ──────
    save_secrets({"demo_kit": {"api_key": "sek-ret-token"}})
    config = LangGraphConfig.from_yaml(str(cfg_path))
    assert config.plugin_config["demo_kit"]["api_key"] == "sek-ret-token"  # NOT "unset"
    assert (agent / "cfg" / "secrets.yaml").exists()

    # ── 4. USE → the registered tool actually runs ───────────────────────────────
    hello = next(t for t in res.tools if getattr(t, "name", "") == "demo_kit_hello")
    assert hello.invoke({"name": "trader"}) == "hello trader"

    # ── 5. UPDATE → re-install at a new commit bumps the locked sha ──────────────
    (repo / "VERSION").write_text("2\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bump")
    summary2 = installer.install(str(repo), force=True)
    assert summary2["resolved_sha"] != summary["resolved_sha"]

    # ── 6. UNINSTALL (purge) → code + lock + the secret are all gone ─────────────
    installer.uninstall("demo_kit", purge=True)
    assert not installed_dir.exists()
    assert installer.list_installed() == []
    leftover = (agent / "cfg" / "secrets.yaml").read_text() if (agent / "cfg" / "secrets.yaml").exists() else ""
    assert "sek-ret-token" not in leftover  # purge dropped the secret too
