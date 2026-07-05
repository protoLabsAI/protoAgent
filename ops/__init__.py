"""The ops layer (ADR 0075 D2) — one function per operation, three faithful projections.

An **op** wraps a `graph/**` / infra core *plus* the orchestration that was REST-only,
so the CLI, the operator MCP, and the HTTP API stop each re-implementing the same glue
and instead become thin adapters:

- CLI adapter → parse argv → call the op → render text/JSON.
- REST route → validate body → call the op → JSON.
- MCP tool / agent tool → the op, wrapped once.

Ops take **plain args + an `OpContext`** (the booted stores/config, injected so ops stay
testable) and return **plain results**; expected failures raise a typed error the adapter
maps to its surface (a tool string, an HTTP status). Ops live in `ops/` — a neutral infra
package that must never import `server` / `operator_api` (import-layering) — so any surface
can call them.

Each op registers metadata via `@op(...)`: its name, whether it **mutates** state, and a
one-line summary. That registry is the single source for the `safe-operator` MCP profile
(read vs. write) and the `GET /api/operations` catalog (ADR 0075 D2/D3/D4) — so those are
derived, not hand-maintained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class OpContext:
    """The handles an op needs, injected rather than reached for. Build it from live
    process state with :meth:`from_state`, or construct it directly with fakes in tests."""

    knowledge_store: Any = None
    graph_config: Any = None

    @classmethod
    def from_state(cls) -> "OpContext":
        """The context for the running instance — reads ``runtime.state.STATE`` (populated
        by ``server.agent_init`` in the live server, ``operator_mcp._boot_stores_only`` in a
        sidecar). Import is local so ``ops`` never hard-depends on a booted process."""
        from runtime.state import STATE

        return cls(knowledge_store=STATE.knowledge_store, graph_config=STATE.graph_config)


@dataclass(frozen=True)
class OpSpec:
    """Registered metadata for one op — the seed of the `safe-operator` profile
    (``mutates``) and the `GET /api/operations` catalog (``name`` / ``summary``)."""

    name: str
    mutates: bool
    summary: str


_REGISTRY: dict[str, OpSpec] = {}


def op(*, name: str, mutates: bool, summary: str) -> Callable:
    """Decorator that records an op's :class:`OpSpec` in the registry and stamps
    ``fn.op_spec``. ``mutates`` is the read/write bit the middle MCP tier keys on:
    ``False`` ops are admissible to ``read-only``/``safe-operator``; ``True`` ops need
    ``full`` (or an explicit safe-operator allowance)."""

    def deco(fn: Callable) -> Callable:
        spec = OpSpec(name=name, mutates=mutates, summary=summary)
        if name in _REGISTRY and _REGISTRY[name] != spec:
            raise ValueError(f"op {name!r} already registered with different metadata")
        _REGISTRY[name] = spec
        fn.op_spec = spec
        return fn

    return deco


def registry() -> dict[str, OpSpec]:
    """A copy of the registered ops, keyed by name — the source for the catalog + profile."""
    return dict(_REGISTRY)


# Every op module — importing one runs its `@op` decorators, which is what populates the
# registry. They're imported lazily elsewhere (a tool / route pulls in just the module it
# needs), so nothing guarantees the WHOLE registry is loaded. `load_all()` forces it, for the
# `GET /api/operations` catalog + `protoagent operations` + the safe-operator profile, which
# must see every op, not just the ones some surface happened to import.
_OP_MODULES = ("ops.knowledge", "ops.plugins", "ops.config", "ops.fleet")


def load_all() -> dict[str, OpSpec]:
    """Import every op module so the registry is complete, then return it. Idempotent — a
    re-import is a cheap no-op. Call before reading :func:`registry` for a full enumeration."""
    import importlib

    for mod in _OP_MODULES:
        importlib.import_module(mod)
    return registry()
