"""A fixture plugin for the testkit suite — exercises the hard cases: a sibling engine
module, a tool module with a module-level relative import + ``@tool``, and a lazy host
import (``graph.goals.types``) inside ``register()``. Host imports stay lazy so loading
the package is host-free."""

from __future__ import annotations


def register(registry):
    """Contribute a tool + a goal verifier + a skill dir + a chat command (host imports lazy)."""
    from graph.goals.types import VerifyResult  # lazy host import — resolved by host_stubs

    from . import tools

    registry.register_tool(tools.summarize)

    def _verify(_cfg):
        return VerifyResult(ok=True, detail="fixture")

    registry.register_goal_verifier("sample:done", _verify)
    registry.register_skill_dir("skills")

    async def _cmd(rest, session_id):
        return f"sample:{rest}"

    registry.register_chat_command("Sample Cmd", _cmd)  # mixed case — slugified to /sample-cmd
    registry.register_late_tool_factory(lambda all_tools, config: None)
