"""Forced-tool-call finalizer for schema-enforced skill outputs.

The runtime-local half of the structured-skill design (ADR-0006 addendum /
#476): the lead agent reasons freely and produces a text answer; when the turn
is for a skill that declares an ``output_schema``, this finalizer reshapes that
answer into the schema via a **forced tool call** (the provider-agnostic
generation primitive — ``response_format`` is unreliable across our model zoo),
validates it, repairs once, and returns the ``protolabs_a2a`` DataPart emit dict.
Returns ``None`` to degrade to text-only (the schema isn't enforceable this turn).

Enforcement lives here (not in ``protolabs_a2a``) because it's our LLM stack —
``protolabs_a2a`` stays the wire/convention layer (``submit_skill_tool`` builds
the tool spec, ``validate_skill_args``/``emit_skill_result`` are pure). Mirrors
jon's reference executor pattern so the fleet's Python runtimes stay consistent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def finalize_structured(
    skill_id: str,
    schema: dict[str, Any],
    mime: str,
    answer_text: str,
    config: Any,
) -> dict[str, Any] | None:
    """Reshape ``answer_text`` into ``schema`` via a forced ``submit_<skill>``
    tool call; validate + one repair; return the ``emit_skill_result`` DataPart
    dict, or ``None`` (degrade to text-only)."""
    import protolabs_a2a as pa

    from graph.llm import create_llm

    tool = pa.submit_skill_tool(skill_id, schema)
    name = pa.skill_tool_name(skill_id)
    # bind the SHARED tool spec (not LangChain with_structured_output, which would
    # build its own tool — we want the fleet-wide submit_<skill> convention) and
    # force tool_choice onto it.
    llm = create_llm(config).bind_tools([tool], tool_choice=name)

    system = (
        f"You are finalizing the result of the '{skill_id}' skill. Reshape the "
        f"answer below into the structured result by calling the {name} tool — "
        f"fill every required field from the answer, and do not invent facts."
    )
    messages: list = [("system", system), ("human", answer_text)]

    async def _call(msgs: list) -> Any | None:
        try:
            resp = await llm.ainvoke(msgs)
        except Exception:  # noqa: BLE001 — any failure ⇒ degrade to text-only
            logger.exception("[structured] %s finalize call failed", skill_id)
            return None
        calls = getattr(resp, "tool_calls", None) or []
        return calls[0]["args"] if calls else None

    args = await _call(messages)
    if args is None:
        return None

    errors = pa.validate_skill_args(args, schema)
    if errors:
        repair = (
            "human",
            "That result was invalid:\n- "
            + "\n- ".join(errors)
            + f"\nCall {name} again with every required field present and corrected.",
        )
        args = await _call([*messages, repair])
        if args is None or pa.validate_skill_args(args, schema):
            logger.warning("[structured] %s still invalid after repair — text-only", skill_id)
            return None

    return pa.emit_skill_result(args, mime)
