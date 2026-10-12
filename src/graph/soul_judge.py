"""Semantic persona-drift judge — the second tier of #1986 (issue #2272).

The **deterministic** tier shipped in #2116: it diffs the live ``SOUL.md`` against its
earliest soul-history snapshot with a ``difflib`` retention ratio, publishes
``persona.drift_detected`` past a threshold, and works. But it measures *text
similarity*, which cannot tell apart the two things an operator actually cares about:

* **the persona was rewritten** — identity changed; the agent is someone else now, and
* **operating instructions accreted into the persona** — identity intact, but SOUL.md has
  silted up with procedure (the CLAUDE.md / AGENTS.md bloat failure).

Both read as a low retention ratio. ``doctrine_leak`` is a *semantic category* judgement,
so it needs a judge — and one judge covers both, which is why #1986 specified this tier
and why it also closes the persona-vs-doctrine follow-up from #1985.

Opt-in (``soul.drift.judge.enabled``), off by default: it is non-deterministic and costs
tokens, so it runs only when the deterministic tier already flagged drift — the cheap
signal decides *whether to look*, the judge decides *what kind*. Never raises: a judge
failure degrades to "no semantic verdict" and leaves the deterministic report intact,
because a curation pass must never take down the maintenance loop that hosts it.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("protoagent.soul-drift")

# Bound the prompt: a persona is normally a page or two, but nothing stops an operator
# (or a runaway edit_soul) from growing it, and a judge prompt must stay predictable.
_MAX_CHARS = 12000

_JUDGE_SYSTEM = (
    "You are auditing an AI agent's persona file (SOUL.md) for drift. You are given the "
    "BASELINE persona and the CURRENT one. Judge two DIFFERENT things and do not conflate "
    "them:\n"
    "1. identity_preserved — is this recognisably the same character? Same role, values, "
    "voice, and stance toward its work. Rewording, tightening, or reordering preserves "
    "identity; becoming a different kind of agent does not.\n"
    "2. doctrine_leak — has OPERATING PROCEDURE accreted into the persona? Step-by-step "
    "instructions, tool usage rules, workflow checklists, environment specifics, project "
    "conventions. These belong in skills or guidelines, not in an identity file. A persona "
    "that merely *describes* how the agent works is fine; one that reads like a runbook is "
    "a leak.\n"
    "Identity can be preserved WHILE doctrine leaks — that is the common case and the one "
    "this audit exists to catch.\n"
    'Reply with ONLY a JSON object: {"drift_score": <0.0-1.0>, "identity_preserved": '
    '<true|false>, "doctrine_leak": <true|false>, "rationale": "<one or two sentences>"} '
    "and nothing else."
)


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS] + "\n…[truncated]"


def _build_prompt(baseline: str, current: str) -> str:
    return f"BASELINE PERSONA:\n{_clip(baseline)}\n\n---\n\nCURRENT PERSONA:\n{_clip(current)}"


def _invoke_judge(prompt: str, model: str | None) -> str:
    """Call the judge model and return its raw reply.

    Isolated so tests can monkeypatch it without a live gateway — the same seam
    ``evals.judge._invoke_grader`` uses.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from graph.llm import create_llm
    from runtime.state import STATE

    config = STATE.graph_config
    if config is None:
        raise RuntimeError("no loaded config — cannot reach the gateway")
    llm = create_llm(config, model_name=model or config.model_name)
    resp = llm.invoke([SystemMessage(_JUDGE_SYSTEM), HumanMessage(prompt)])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def _parse(raw: str) -> dict | None:
    """Pull the verdict out of the judge's reply, tolerant of fences/prose."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        log.warning("[soul-drift] judge returned no JSON: %r", (raw or "")[:160])
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log.warning("[soul-drift] judge returned bad JSON: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    try:
        score = float(data.get("drift_score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return {
        # Clamp: a judge that answers 1.5 or -0.2 must not poison a threshold comparison.
        "drift_score": round(min(1.0, max(0.0, score)), 4),
        # Default identity_preserved TRUE on a missing key: absence of a verdict is not
        # evidence the persona was replaced, and a false alarm here is the expensive one.
        "identity_preserved": bool(data.get("identity_preserved", True)),
        "doctrine_leak": bool(data.get("doctrine_leak", False)),
        "rationale": str(data.get("rationale", "")).strip()[:600],
    }


def judge_soul_drift(baseline: str, current: str, *, model: str | None = None) -> dict | None:
    """Semantic verdict on a persona diff, or ``None`` when no verdict is available.

    Returns ``{drift_score, identity_preserved, doctrine_leak, rationale}``. ``None``
    means the judge couldn't be reached or didn't answer usably — a distinct outcome from
    "no drift", so a caller can report the deterministic tier alone rather than implying a
    clean semantic bill of health it never got.
    """
    if not (baseline or "").strip() or not (current or "").strip():
        return None
    try:
        raw = _invoke_judge(_build_prompt(baseline, current), model)
    except Exception:  # noqa: BLE001 — a curation pass must never break its host loop
        log.exception("[soul-drift] judge call failed")
        return None
    return _parse(raw)
