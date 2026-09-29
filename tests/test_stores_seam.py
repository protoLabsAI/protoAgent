"""Seam guard for the ``server/stores.py`` extraction (#3829).

The checkpointer, the per-agent store builders (inbox / background / activity), the
telemetry / metrics / ledger stores and the inbox now-recovery worker moved out of
``server/agent_init.py`` and are re-exported there, so ``agent_init.<name>`` still
RESOLVES — but a monkeypatch on ``agent_init`` no longer INTERCEPTS a collaborator the
moved code calls by bare name: it resolves in ``stores``'s globals. Such a patch is dead,
and the test around it can pass silently against the real thing. This scans the suite so
a stale target fails loudly.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import server.agent_init as agent_init
import server.stores as stores

# Names whose only live binding (as a callee / read) is in stores. Deliberately absent:
# the ``_build_*`` builders and ``_start_inbox_now_recovery_once`` — agent_init's boot /
# reload path (and server/__init__) call them by bare name, so a patch on agent_init (or
# server) DOES intercept those callers. ``instance_paths`` / ``agent_name`` are not listed
# because agent_init still uses them itself; to fake them for a moved builder, patch
# ``server.stores``.
_MOVED_COLLABORATORS = {
    "_agent_store_db",
    "_resolve_checkpoint_db",
    "recover_pending_now_inbox_items",
    "_on_work_terminal",
    "_AGENT_DB",
    "_INBOX_NOW_RECOVERY_STARTED",
    "_INBOX_NOW_RECOVERY_BATCH_LIMIT",
    "_INBOX_NOW_RECOVERY_RETRY_AFTER_S",
    "_INBOX_NOW_RECOVERY_MAX_ATTEMPTS",
}

# Re-exported from agent_init by identity. The latch ``_INBOX_NOW_RECOVERY_STARTED`` is
# deliberately NOT: a re-exported bool is a stale snapshot, so it has ONE home.
_RE_EXPORTED = (_MOVED_COLLABORATORS - {"_INBOX_NOW_RECOVERY_STARTED"}) | {
    "_build_checkpointer",
    "_build_inbox_store",
    "_build_background_manager",
    "_build_activity_log",
    "_build_telemetry_store",
    "_build_metrics_store",
    "_build_ledger_store",
    "_start_inbox_now_recovery_once",
}

_TESTS = Path(__file__).resolve().parent


def _agent_init_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "server.agent_init"}
        elif isinstance(node, ast.ImportFrom) and node.module == "server":
            aliases |= {a.asname or a.name for a in node.names if a.name == "agent_init"}
    return aliases


def test_no_test_patches_a_moved_collaborator_on_agent_init():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _agent_init_aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"setattr", "object", "patch"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target.startswith("server.agent_init.") and target.rsplit(".", 1)[1] in _MOVED_COLLABORATORS:
                    stale.append(f"{path.name}:{node.lineno} {target}")
            elif (
                isinstance(first, ast.Name)
                and first.id in aliases
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MOVED_COLLABORATORS
            ):
                stale.append(f"{path.name}:{node.lineno} {first.id}.{node.args[1].value}")
    assert not stale, "patch these on server.stores, not agent_init (#3829): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    """agent_init / server re-export the moved names by identity (no stale copies); the
    now-recovery latch has ONE home."""
    import server

    for name in _RE_EXPORTED:
        assert getattr(agent_init, name) is getattr(stores, name), name
    for name in (
        "_build_activity_log",
        "_build_checkpointer",
        "_build_inbox_store",
        "_build_telemetry_store",
        "_resolve_checkpoint_db",
        "_start_inbox_now_recovery_once",
    ):
        assert getattr(server, name) is getattr(stores, name), name
    assert not hasattr(agent_init, "_INBOX_NOW_RECOVERY_STARTED")


def test_boot_and_reload_call_the_builders_through_agent_init():
    """init/reload call the store builders by BARE name, so they resolve in agent_init's
    globals at call time and a patch on ``agent_init._build_inbox_store`` intercepts them."""
    init_names = set(agent_init._init_langgraph_agent.__code__.co_names)
    assert {
        "_build_checkpointer",
        "_build_inbox_store",
        "_build_activity_log",
        "_build_background_manager",
        "_build_metrics_store",
        "_build_ledger_store",
    } <= init_names
    reload_fn = getattr(agent_init._reload_langgraph_agent, "__wrapped__", agent_init._reload_langgraph_agent)
    assert "_build_inbox_store" in reload_fn.__code__.co_names
    assert agent_init._init_langgraph_agent.__globals__ is vars(agent_init)


def test_moved_code_resolves_collaborators_in_stores(monkeypatch):
    """The recovery worker reads the latch and calls ``recover_pending_now_inbox_items`` in
    stores' globals, so a patch there intercepts it even via the agent_init re-export."""
    from runtime.state import STATE

    calls: list[str] = []

    async def _recover():
        calls.append("recover")

    async def _drive():
        monkeypatch.setattr(STATE, "main_loop", asyncio.get_running_loop(), raising=False)
        agent_init._start_inbox_now_recovery_once()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    monkeypatch.setattr(stores, "_INBOX_NOW_RECOVERY_STARTED", False)
    monkeypatch.setattr(stores, "recover_pending_now_inbox_items", _recover)
    monkeypatch.setattr(STATE, "inbox_store", object(), raising=False)
    monkeypatch.setattr(STATE, "inbox_now_delivery", lambda _item: True, raising=False)
    asyncio.run(_drive())
    assert calls == ["recover"]
    assert stores._INBOX_NOW_RECOVERY_STARTED is True
