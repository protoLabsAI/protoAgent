"""DelegateRegistry — parse the ``delegates`` config into dispatchable targets.

Rebuilt from config on every graph build / hot-reload (ADR 0025), so editing the
``delegates`` section + Save & Reload swaps the roster live — protoAgent's native
equivalent of ORBIS's ``registry.reload()`` + session refresh.
"""

from __future__ import annotations

import logging

from .adapters import ADAPTERS, Delegate, DelegateError

logger = logging.getLogger("protoagent.plugins.delegates")


class DelegateRegistry:
    def __init__(self, raw_delegates: list | None = None):
        self._items: dict[str, Delegate] = {}
        for raw in raw_delegates or []:
            self._add(raw)

    def _add(self, raw) -> None:
        if not isinstance(raw, dict):
            logger.warning("[delegates] ignoring non-mapping entry: %r", raw)
            return
        dtype = str(raw.get("type", "")).strip()
        adapter = ADAPTERS.get(dtype)
        if adapter is None:
            logger.warning(
                "[delegates] %s: unknown type %r (want one of %s) — skipped",
                raw.get("name"),
                dtype,
                ", ".join(ADAPTERS),
            )
            return
        try:
            d = adapter.parse(raw)
        except DelegateError as exc:
            logger.warning("[delegates] dropping invalid delegate: %s", exc)
            return
        if d.name in self._items:
            logger.warning("[delegates] duplicate name %r — keeping first", d.name)
            return
        self._items[d.name] = d

    def names(self) -> list[str]:
        return list(self._items)

    def get(self, name: str) -> Delegate | None:
        return self._items.get(name)

    def listing(self) -> str:
        """Human/LLM-facing one-liner per delegate (for the tool description)."""
        return "; ".join(
            f"`{d.name}` ({d.type}{' — ' + d.description if d.description else ''})" for d in self._items.values()
        )

    def roster(self) -> list[dict]:
        """Structured one-entry-per-delegate roster (for the ``list_agents`` tool)."""
        return [
            {"name": d.name, "type": d.type, "description": d.description, "url": d.url} for d in self._items.values()
        ]

    async def dispatch(self, name: str, query: str, *, item_id: str | None = None, raw: bool = False) -> str:
        """Dispatch ``query`` to the named delegate.

        ``item_id`` is the work-item identity for adapters that manage a git
        lifecycle (ADR 0076); identity-less adapters ignore it. ``raw=True`` bypasses
        the managed-git lifecycle for programmatic callers that consume the reply as
        DATA (e.g. the coder ladder's candidate generation, ADR 0064) — no branch, no
        commit, no PR, no claim; just the coder's text."""
        d = self._items.get(name)
        if d is None:
            raise DelegateError(f"unknown delegate {name!r}. Configured: {', '.join(self._items) or '(none)'}.")
        if raw and d.manage_git:
            import dataclasses

            d = dataclasses.replace(d, manage_git=False)
        return await ADAPTERS[d.type].dispatch(d, query, item_id=item_id)
