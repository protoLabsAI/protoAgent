"""Config ops (ADR 0075 D2) — read + set config over one op the REST settings route and the
`protoagent config` CLI share.

`set` applies a nested config-updates dict: **live** (a running server) → the injected
`apply_settings` rebuilds the agent (`server.agent_init._apply_settings_changes`, the same
call the settings route always made, offloaded off the event loop per #497); **disk-only**
(a CLI with no server) → merge into config.yaml and save, so `protoagent config set` works
headless. `get` reads the live config, or the on-disk doc when no agent is running.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from ops import OpContext, op


@dataclass
class ConfigSetResult:
    ok: bool
    messages: list[str]
    reloaded: bool  # True when a live agent was rebuilt; False for a disk-only write


@op(
    name="config.set",
    mutates=True,
    summary="Apply config updates — hot-reload the live agent, or write config.yaml on disk.",
)
async def set_config(
    updates: dict,
    *,
    ctx: OpContext | None = None,
    apply_settings: Callable[[dict], tuple[bool, list]] | None = None,
) -> ConfigSetResult:
    """Apply ``updates`` (a nested config dict). With ``apply_settings`` (a live server),
    rebuild the agent off the event loop; without it, write config.yaml on disk."""
    if not updates:
        return ConfigSetResult(ok=True, messages=["no changes"], reloaded=False)
    if apply_settings is not None:
        # Heavy — the reload recompiles the graph — so keep it off the event loop (#497).
        ok, messages = await asyncio.to_thread(apply_settings, updates)
        return ConfigSetResult(ok=bool(ok), messages=list(messages or []), reloaded=bool(ok))

    # Disk-only (a headless CLI with no running server): merge into the YAML doc + save.
    from graph.config_io import apply_updates_to_yaml, config_yaml_path, load_yaml_doc, save_yaml_doc

    def _write() -> None:
        path = config_yaml_path()
        doc = load_yaml_doc(path)
        doc = apply_updates_to_yaml(doc if isinstance(doc, dict) else {}, updates)
        save_yaml_doc(doc, path)

    await asyncio.to_thread(_write)
    return ConfigSetResult(ok=True, messages=[f"wrote {', '.join(sorted(updates))} to config.yaml"], reloaded=False)


@op(
    name="config.get",
    mutates=False,
    summary="Read the live config (or the on-disk config.yaml when no agent is running).",
)
async def get_config(*, ctx: OpContext | None = None) -> dict:
    """The effective config as a plain dict — the live ``graph_config`` when there's a
    running agent, else the on-disk config.yaml."""
    from graph.config_io import config_to_dict

    cfg = ctx.graph_config if ctx else None
    if cfg is not None:
        return config_to_dict(cfg)
    from graph.config_io import config_yaml_path, load_yaml_doc

    doc = load_yaml_doc(config_yaml_path())
    return doc if isinstance(doc, dict) else {}
