"""Candidate generation over the delegate registry (ADR 0064).

``coder`` does not own a model client — it **composes the `delegates` plugin**
(ADR 0025), exactly as ``projectBoard-plugin`` does. A generator dispatches a
prompt to a named delegate (an ``openai`` model endpoint for the caller-tests
path; an ``acp`` coding agent for repo work) and returns one candidate solution.

We rebuild a :class:`DelegateRegistry` from ``merged_delegates()` per call so a
roster edit hot-swaps with no restart (the same read ``delegate_to`` does). The
returned callable matches the ladder's ``generate(prompt, *, feedback)`` contract.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

log = logging.getLogger("protoagent.plugins.coder")

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

# best-of-k dispatches `generate` CONCURRENTLY (solve.py asyncio.gather). An `acp`
# delegate is a single stateful session (one cached AcpClient keyed by name+workdir);
# concurrent `prompt()`s on it interleave and corrupt each other — its own docstring
# says callers must serialize. So we hold a per-delegate lock for stateful (acp)
# delegates; stateless ones (openai/a2a: fresh client per dispatch) stay parallel.
# True independent-parallel acp attempts need a worktree per candidate — the P2 path.
_STATEFUL_TYPES = {"acp"}
_DISPATCH_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(name: str) -> asyncio.Lock:
    lk = _DISPATCH_LOCKS.get(name)
    if lk is None:
        lk = _DISPATCH_LOCKS[name] = asyncio.Lock()
    return lk


def extract_code(text: str) -> str:
    """Pull the solution out of a model reply: the largest fenced block, else the
    whole reply (a bare-code reply with no fences)."""
    blocks = _FENCE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip()
    return (text or "").strip()


def _prompt(task: str, *, solution_name: str, feedback: Optional[str]) -> str:
    parts = [
        f"Implement a solution to the task below as a Python module named `{solution_name}.py`.",
        "Return ONLY the module code in a single ```python fenced block — no prose, no tests.",
        "",
        "## Task",
        task.strip(),
    ]
    if feedback:
        parts += [
            "",
            "## Your previous attempt failed these tests — fix exactly these:",
            feedback.strip(),
        ]
    return "\n".join(parts)


def make_delegate_generator(delegate_name: str, *, solution_name: str = "solution"):
    """Build a ``generate(task, *, feedback) -> code`` that dispatches to the named
    delegate. Raises at call time (surfaced as a tool error) if the delegate is
    absent so a misconfigured roster fails loudly, not silently."""

    async def generate(task: str, *, feedback: Optional[str] = None) -> str:
        from plugins.delegates.registry import DelegateRegistry

        try:
            from plugins.delegates.store import merged_delegates

            roster = merged_delegates()
        except Exception:  # noqa: BLE001 — config read is best-effort
            log.exception("[coder] reading delegates config failed")
            roster = []
        reg = DelegateRegistry(roster)
        d = reg.get(delegate_name)
        if d is None:
            available = ", ".join(reg.names()) or "(none)"
            raise ValueError(
                f"coder: delegate {delegate_name!r} not found. Declare it under `delegates` "
                f"(an openai model endpoint, or an acp coder). Available: {available}"
            )
        prompt = _prompt(task, solution_name=solution_name, feedback=feedback)
        if d.type in _STATEFUL_TYPES:
            async with _lock_for(delegate_name):  # serialize concurrent best-of-k on one session
                reply = await reg.dispatch(delegate_name, prompt)
        else:
            reply = await reg.dispatch(delegate_name, prompt)
        return extract_code(reply)

    return generate
