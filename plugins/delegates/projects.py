"""Per-call project scoping for ACP delegates — ``delegate_to(project=…)``.

An ACP delegate is configured with ONE ``workdir``. ``project`` lets the lead hand a
focused job to that same coder inside a different *registered* project for one call.
The project is resolved through the fenced fs-project registry the filesystem tools
use (ADR 0007 / ADR 0095 — ``tools.fs_tools.live_project_registry``), so the model can
only NAME a project the operator already registered; it never supplies a path.

What changes for the call is the workdir and nothing else: command, args and env stay
the operator's (``delegates[]`` entries are executables — the model may choose among
them, never define one). The registry applies the scope on a ``dataclasses.replace``
copy, so the configured roster entry is never mutated.

``project_scope`` carries the resolved scope across ``graph.mention_op`` — the host-free
room helper can't take a new ``dispatch`` argument — the same way
``conversations.origin_session`` does for the originating session (#3362).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .adapters import DelegateError


@dataclass(frozen=True)
class ProjectScope:
    """One resolved, fenced project: its registry name, absolute root, and write flag."""

    name: str
    root: str
    write: bool


def resolve(project: str) -> ProjectScope:
    """Resolve a registered project name to its fenced root.

    Raises ``DelegateError`` for an unknown project (listing the known ones) and for a
    READ-ONLY project (``write: false``). Refusing — rather than forcing the ACP
    ``readonly`` permission ceiling — is deliberate: the ceiling only bites when the
    child *asks* before it edits, and an ACP agent running in its own auto-accept /
    bypass mode (Claude Code honours the operator's ``~/.claude`` settings) never asks.
    A read-only fence must not rest on the child's cooperation.
    """
    from runtime.state import STATE
    from tools.fs_tools import live_project_registry

    name = str(project or "").strip()
    registry = live_project_registry(getattr(STATE, "graph_config", None))
    try:
        root = registry.resolve(name, ".")
    except ValueError:
        known = ", ".join(
            f"{n} ({'rw' if registry.get(n).write else 'ro'})" for n in registry.names()
        ) or "(none — register one in Settings ▸ Projects, and filesystem.enabled must be on)"
        raise DelegateError(f"unknown project {name!r}. Registered projects: {known}.") from None
    proj = registry.get(name)
    if not proj.write:
        raise DelegateError(
            f"project {name!r} is read-only (write: false) — a coding delegate can't be sent "
            "into it. Ask the operator to make it read-write, or read it yourself with the fs tools."
        )
    if not root.is_dir():
        raise DelegateError(f"project {name!r} root does not exist: {root}")
    return ProjectScope(name=name, root=str(root), write=True)


_SCOPE: ContextVar[ProjectScope | None] = ContextVar("protoagent_delegate_project_scope", default=None)


@contextmanager
def project_scope(scope: ProjectScope | None):
    """Bind ``scope`` for dispatches made inside the block (the room path)."""
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope() -> ProjectScope | None:
    return _SCOPE.get()
