"""What the agent is told a directive is (#5367).

The ledger renders every in-force directive every turn as the operator's words, so the
tool and the operating model must keep design answers, facts and agent paraphrase out of
it. These pin the wording that draws that line; the budgets live in test_prompt_budgets.
"""

from __future__ import annotations

from graph.prompts import _build_operating_model
from tools.directive_tools import DIRECTIVE_TOOL_NAMES, build_directive_tools


def _record_doc() -> str:
    return next(t for t in build_directive_tools() if t.name == "record_directive").description


def test_record_directive_defines_a_directive_by_its_effect_on_a_later_turn():
    doc = _record_doc()
    assert "LATER turn" in doc
    assert "would a later turn act differently" in doc


def test_record_directive_excludes_answers_to_the_agents_own_design_questions():
    doc = _record_doc()
    assert "NOT directives: answers to questions you asked" in doc
    assert "spec, issue or" in doc


def test_conditions_carry_only_the_operators_stated_rules():
    doc = _record_doc()
    assert "only the trigger or stop rules the operator" in doc
    assert "never rules, caveats or design detail of your own" in doc


def test_record_directive_asks_for_links_or_a_prompt_fulfil():
    doc = _record_doc()
    assert "Link whenever the work is trackable" in doc
    assert "fulfil_directive" in doc


def test_operating_model_draws_the_same_line():
    model = _build_operating_model(frozenset(DIRECTIVE_TOOL_NAMES))
    assert "binds a later turn" in model
    assert "Answers to your own design questions belong in the spec, not the ledger." in model
    assert "linking the work it covers" in model
    assert "approves or instructs something beyond this turn" not in model
