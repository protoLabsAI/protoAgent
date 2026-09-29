"""Seam guard for the ``plugins/delegates/adapters.py`` split (#3831).

The A2A wire helpers + ``A2aAdapter`` moved to ``plugins/delegates/a2a.py``, ``AcpAdapter``
+ ``_mark_incomplete`` to ``plugins/delegates/acp_adapter.py``, and the shared model / base
class to ``plugins/delegates/base.py``. ``adapters`` re-exports every moved name, so
``adapters.<name>`` still RESOLVES — but a monkeypatch on ``adapters`` no longer
INTERCEPTS anything the moved code reads by bare name: that resolves in the new home
module's globals. Such a patch is dead, and the test around it can pass silently against
the real thing. This scans the suite so a stale target fails loudly.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from plugins.delegates import a2a, acp_adapter, adapters, base

_HOMES = {"a2a": a2a, "acp_adapter": acp_adapter, "base": base}


def _module_names(mod) -> set[str]:
    """Top-level names DEFINED in ``mod`` (not the ones it imports)."""
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out - {"logger"}  # each module binds the SAME named logger


_MOVED = {name: home for home, mod in _HOMES.items() for name in _module_names(mod)}

_TESTS = Path(__file__).resolve().parent


def _adapters_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "plugins.delegates.adapters"}
        elif isinstance(node, ast.ImportFrom) and node.module == "plugins.delegates":
            aliases |= {a.asname or a.name for a in node.names if a.name == "adapters"}
    return aliases


def test_no_test_patches_a_moved_name_on_adapters():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _adapters_aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"setattr", "object", "patch", "delattr"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target.startswith("plugins.delegates.adapters.") and target.rsplit(".", 1)[1] in _MOVED:
                    stale.append(f"{path.name}:{node.lineno} {target} (→ {_MOVED[target.rsplit('.', 1)[1]]})")
            elif (
                isinstance(first, ast.Name)
                and first.id in aliases
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MOVED
            ):
                stale.append(
                    f"{path.name}:{node.lineno} {first.id}.{node.args[1].value} (→ {_MOVED[node.args[1].value]})"
                )
    assert not stale, "patch these on their new home module, not adapters (#3831): " + ", ".join(stale)


def test_every_moved_name_is_re_exported_by_identity():
    """``adapters`` re-exports each moved name as the SAME object — no stale copies, and
    module-level state (``_clamp_warned``, ``_DETACHED_DELEGATION``) has ONE home."""
    assert _MOVED, "no moved names discovered"
    for name, home in _MOVED.items():
        assert getattr(adapters, name) is getattr(_HOMES[home], name), name
    assert adapters._clamp_warned is a2a._clamp_warned
    assert adapters._DETACHED_DELEGATION is a2a._DETACHED_DELEGATION


def test_adapters_registry_holds_the_moved_classes():
    assert type(adapters.ADAPTERS["a2a"]) is a2a.A2aAdapter
    assert type(adapters.ADAPTERS["acp"]) is acp_adapter.AcpAdapter
    assert type(adapters.ADAPTERS["openai"]) is adapters.OpenAiAdapter
    assert all(isinstance(a, base.Adapter) for a in adapters.ADAPTERS.values())


def test_moved_a2a_code_resolves_its_state_at_call_time(monkeypatch, caplog):
    """A patch on ``a2a`` (where the retargeted tests now patch) is seen by the moved code."""
    import asyncio

    seen: list = []
    monkeypatch.setattr(a2a, "_peer_usage_row", lambda result, delegate: seen.append(delegate))
    asyncio.run(a2a._bill_peer_usage({}, "peerX"))
    assert seen == ["peerX"]

    monkeypatch.setattr(a2a, "_SHORT_REPLY_MIN_ELAPSED_S", 0.0)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins.delegates"):
        a2a._warn_if_suspiciously_short("peerX", "ok", elapsed_s=0.1)
    assert any("#3085" in m for m in caplog.messages)

    fresh: set[str] = set()
    monkeypatch.setattr(a2a, "_clamp_warned", fresh)
    a2a._note_clamped("peerY", ["costUsd"])
    assert fresh == {"peerY"}
