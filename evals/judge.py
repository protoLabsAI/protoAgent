"""LLM-judge rubric scorer for eval cases (ADR 0012 §2.5).

Some things a good answer must do can't be checked with a substring or an
audit-log entry — "is the deep-research report actually *balanced*?", "is the
confidence *earned*?". For those, a grader model scores the output against a
short rubric of yes/no criteria and the case passes above a threshold.

Used by ``evals.runner`` when a case carries a ``verify_rubric`` block::

    "verify_rubric": {
      "criteria": [
        "Presents opposing/critical perspectives, not just the consensus",
        "Has a counterpoints or caveats section",
        "States a confidence level that is justified, not just asserted"
      ],
      "threshold": 0.66,          # fraction of criteria that must be met
      "model": "protolabs/reasoning"   # optional grader override
    }

The grader is non-deterministic + costs tokens — treat scores as a tracked
signal (trend across models), with the deterministic channels (audit /
substring / KB) as the hard pass/fail. The grader model defaults to
``$EVAL_JUDGE_MODEL`` then the agent's configured model.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class RubricScore:
    score: float                              # fraction of criteria met, 0..1
    met: dict[str, bool] = field(default_factory=dict)
    reasons: str = ""
    error: str | None = None


_GRADER_SYSTEM = (
    "You are a strict, fair evaluation grader. You are given an OUTPUT produced "
    "by an AI agent and a RUBRIC of independent yes/no criteria. For each "
    "criterion decide whether the OUTPUT clearly meets it. Be skeptical: only "
    "mark met=true when the OUTPUT genuinely satisfies the criterion, not when "
    "it merely gestures at it. Reply with ONLY a JSON object of the form "
    '{"criteria": [{"criterion": "<verbatim>", "met": true|false, '
    '"why": "<one line>"}]} and nothing else.'
)


def _build_prompt(output: str, criteria: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    # Truncate very long outputs so the grader prompt stays bounded.
    snippet = output if len(output) <= 12000 else output[:12000] + "\n…[truncated]"
    return f"RUBRIC:\n{numbered}\n\nOUTPUT:\n{snippet}"


def _invoke_grader(prompt: str, model: str | None) -> str:
    """Call the grader model and return its raw text reply.

    Isolated so the eval runner can monkeypatch it in tests without a live
    gateway. Reuses the agent's gateway plumbing via ``graph.llm.create_llm``.
    """
    from graph.config import LangGraphConfig
    from graph.llm import create_llm
    from langchain_core.messages import HumanMessage, SystemMessage

    config = LangGraphConfig.from_yaml(
        os.environ.get("PROTOAGENT_CONFIG", "config/langgraph-config.yaml")
    )
    grader_model = model or os.environ.get("EVAL_JUDGE_MODEL") or config.model_name
    llm = create_llm(config, model_name=grader_model)
    resp = llm.invoke([SystemMessage(_GRADER_SYSTEM), HumanMessage(prompt)])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def _parse(raw: str, criteria: list[str]) -> RubricScore:
    """Pull the JSON verdict out of the grader's reply (tolerant of code
    fences / surrounding prose)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return RubricScore(score=0.0, error=f"no JSON in grader reply: {raw[:160]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        return RubricScore(score=0.0, error=f"bad JSON from grader: {e}")

    rows = data.get("criteria") or []
    met: dict[str, bool] = {}
    reasons: list[str] = []
    for i, c in enumerate(criteria):
        row = rows[i] if i < len(rows) else {}
        is_met = bool(row.get("met"))
        met[c] = is_met
        why = str(row.get("why", "")).strip()
        reasons.append(f"[{'✓' if is_met else '✗'}] {c}" + (f" — {why}" if why else ""))
    score = (sum(met.values()) / len(criteria)) if criteria else 0.0
    return RubricScore(score=score, met=met, reasons="\n".join(reasons))


def score_rubric(output: str, criteria: list[str], *, model: str | None = None) -> RubricScore:
    """Grade ``output`` against ``criteria``; returns a RubricScore (0..1)."""
    if not criteria:
        return RubricScore(score=1.0)
    try:
        raw = _invoke_grader(_build_prompt(output, criteria), model)
    except Exception as e:  # noqa: BLE001 — never crash the eval run on a grader error
        return RubricScore(score=0.0, error=f"grader call failed: {e!r}")
    return _parse(raw, criteria)
