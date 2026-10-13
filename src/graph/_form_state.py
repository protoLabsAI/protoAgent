"""Reload-stable store for pending plugin form callbacks (#2889).

``graph.slash_commands`` holds a form's ``on_submit`` closure between the open
(``run_plugin_chat_command``) and the submit (``POST /api/chat/commands/submit``)
HTTP round-trip. That dict used to be a module global of ``slash_commands`` — a
plugin reload that re-imported the module recreated it EMPTY, so every form
opened before the reload could only ever answer "This form has expired."

The dict therefore lives on a synthetic holder anchored in ``sys.modules`` under
a key the import machinery never re-executes (the projectBoard-plugin#178
coder-monitor pattern): re-importing this module — or ``graph.slash_commands`` —
hands back the SAME dict instance, so pending forms survive the reload. Entries
are only ever attribute-accessed (never isinstance-checked), so instances built
before a reload keep working with post-reload code.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass


@dataclass
class PendingPluginForm:
    on_submit: object  # async (answers: dict, session_id: str) -> str | dict | None
    session_id: str
    created: float  # time.monotonic() at registration — for TTL reaping


# Not a real module — a bare namespace parked in sys.modules purely because that
# mapping is process-wide and survives any re-import of protoagent's own modules.
_ANCHOR = "protoagent._plugin_form_state"


def form_callbacks() -> dict[str, PendingPluginForm]:
    """The process-wide ``callback_id → PendingPluginForm`` map — the same dict
    instance on every call, no matter how often the importing modules reload."""
    holder = sys.modules.get(_ANCHOR)
    if holder is None:
        holder = types.ModuleType(_ANCHOR)
        holder.callbacks = {}
        sys.modules[_ANCHOR] = holder
    return holder.callbacks
