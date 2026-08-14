"""BUNDLE lifecycle smoke (ADR 0040/0049, #2724) — the bundle counterpart to
``test_plugin_lifecycle_smoke`` (#912).

Drives one host agent through a bundle's whole life — install (fan-out + provenance)
→ enable (declared set) → seed (config defaults + mcp) → use → UPDATE (re-pin a moved
member, retire a dropped one, #2718) → UNINSTALL (one action, shared members kept) —
through the real layers: installer, loader, config/mcp seeding. The per-layer tests
check each step in isolation; this is the regression net for the seams between them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph.plugins import installer


def _git(cwd: Path, *args: str) -> None:
    # maintenance.auto=false / gc.auto=0 — fixture repos stay inert (#1600).
    subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _member_repo(root: Path, pid: str) -> Path:
    repo = root / f"src-{pid}"
    repo.mkdir(parents=True)
    (repo / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid}\nversion: 0.1.0\ndescription: bundle smoke member\n"
    )
    (repo / "__init__.py").write_text(
        "from langchain_core.tools import tool\n"
        "def register(registry):\n"
        "    @tool\n"
        f"    def {pid}_ping() -> str:\n"
        '        """ping"""\n'
        f"        return 'pong from {pid}'\n"
        f"    registry.register_tool({pid}_ping)\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def _write_bundle_manifest(repo: Path, members: dict[str, Path], enabled: list[str]) -> None:
    lines = ["id: smoke_stack", "name: Smoke Stack", "description: bundle lifecycle smoke", "plugins:"]
    for mid, path in members.items():
        lines.append(f"  - {{ id: {mid}, url: {path} }}")
    lines.append(f"enabled: [{', '.join(enabled)}]")
    lines += [
        "config:",
        "  member_a: { greeting: hello }",
        "mcp:",
        "  - template: { name: smokesrv, transport: stdio, command: echo, args: ['${flag}'] }",
        "    inputs:",
        "      - { key: flag, default: on }",
    ]
    (repo / "protoagent.bundle.yaml").write_text("\n".join(lines) + "\n")


def _bundle_repo(root: Path, members: dict[str, Path], enabled: list[str]) -> Path:
    repo = root / "src-bundle"
    repo.mkdir(parents=True)
    _write_bundle_manifest(repo, members, enabled)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """One host agent's data area — install dir, lock, config — all temp (the same
    layout the single-plugin smoke models)."""
    import graph.config_io as cio

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(cfg / "plugins"))
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: cfg / "secrets.yaml")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: cfg / "langgraph-config.yaml")
    return tmp_path


def test_bundle_lifecycle_single_agent(agent, monkeypatch):
    from graph.config import LangGraphConfig
    from graph.plugins import loader as plugin_loader
    from graph.plugins.installer import bundle_config_overlay
    from graph.plugins.loader import load_plugins
    from graph.workspaces.manager import apply_bundle_mcp_servers

    cfg_path = agent / "cfg" / "langgraph-config.yaml"

    # ── 1. INSTALL — fan-out + one provenance row ────────────────────────────────
    a = _member_repo(agent, "member_a")
    b = _member_repo(agent, "member_b")
    bundle = _bundle_repo(agent, {"member_a": a, "member_b": b}, enabled=["member_a", "member_b"])
    summary = installer.install(str(bundle))
    assert summary["bundle"] == "smoke_stack"
    assert {p["id"] for p in summary["installed"]} == {"member_a", "member_b"}
    row = installer.bundle_entry("smoke_stack")
    assert set(row["plugins"]) == {"member_a", "member_b"} and row["resolved_sha"]

    # ── 2. ENABLE the declared set → both members load, tools registered ─────────
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [installer.live_plugins_dir()])
    cfg_path.write_text("plugins:\n  enabled: [member_a, member_b]\n")
    res = load_plugins(LangGraphConfig.from_yaml(str(cfg_path)))
    by_id = {m["id"]: m for m in res.meta}
    assert by_id["member_a"]["loaded"] and by_id["member_b"]["loaded"]

    # ── 3. SEED — config defaults overlay + declared mcp: server ─────────────────
    overlay = bundle_config_overlay(row["config"], {})
    assert overlay == {"member_a": {"greeting": "hello"}}  # defaults land…
    assert bundle_config_overlay(row["config"], {"member_a": {"greeting": "hi"}}) == {}  # …operator wins
    seeded = apply_bundle_mcp_servers(cfg_path, installer.lock_path(), {})
    assert seeded == ["smokesrv"]
    assert "smokesrv" in cfg_path.read_text()

    # ── 4. USE — a member's tool actually runs ───────────────────────────────────
    ping = next(t for t in res.tools if getattr(t, "name", "") == "member_a_ping")
    assert ping.invoke({}) == "pong from member_a"

    # ── 5. UPDATE (#2718) — member_a moved, member_b dropped from the manifest ───
    (a / "__init__.py").write_text(
        "from langchain_core.tools import tool\n"
        "def register(registry):\n"
        "    @tool\n"
        "    def member_a_ping() -> str:\n"
        '        """ping"""\n'
        "        return 'pong v2'\n"
        "    registry.register_tool(member_a_ping)\n"
    )
    _git(a, "add", "-A")
    _git(a, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2")
    old_sha = next(e for e in installer._read_lock()["plugins"] if e["id"] == "member_a")["resolved_sha"]
    _write_bundle_manifest(bundle, {"member_a": a}, enabled=["member_a"])  # b dropped
    _git(bundle, "add", "-A")
    _git(bundle, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "drop b, bump a")

    before = list(row["plugins"])
    installer.install(str(bundle), force=True, by="cli-update-bundle:smoke_stack")
    new_sha = next(e for e in installer._read_lock()["plugins"] if e["id"] == "member_a")["resolved_sha"]
    assert new_sha != old_sha  # the moved member re-pinned
    dropped = installer.orphaned_bundle_members("smoke_stack", before)
    assert dropped == ["member_b"]
    for pid in dropped:
        installer.uninstall(pid)
    assert not (installer.live_plugins_dir() / "member_b").exists()
    # …and the freshly pulled code is what loads now
    res = load_plugins(LangGraphConfig.from_yaml(str(cfg_path)))
    ping = next(t for t in res.tools if getattr(t, "name", "") == "member_a_ping")
    assert ping.invoke({}) == "pong v2"

    # ── 6. UNINSTALL — one action; nothing dangles ───────────────────────────────
    rep = installer.uninstall_bundle("smoke_stack")
    assert rep["removed_members"] == ["member_a"]
    assert installer.bundle_entry("smoke_stack") is None
    assert installer.list_installed() == []
    assert not (installer.live_plugins_dir() / "member_a").exists()
