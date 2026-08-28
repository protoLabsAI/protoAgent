"""Prompt-contract budgets (ADR 0108 D3/D8) — the four prompt contracts stay honest and
stay small.

Every figure here is documented in ``docs/explanation/prompt-contracts.md``. A ceiling is
the measured static size × ~1.2, rounded to a readable number: enough headroom for wording
edits, tight enough that a new roster entry, a new doctrine paragraph, or a duplicated
section fails CI with the actual size printed. Raise a ceiling only together with an
update to that page explaining why.

The SOUL is a tiny fixture and excluded from every measurement — it is operator content
with no engineering ceiling. Projects and delegates are absent, so the optional
``Managed projects`` / ``Collaboration`` sections never enter the numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.prompts import (
    _GOAL_TOOLS,
    _SCHEDULE_TOOLS,
    _TASK_TOOLS,
    _WAIT_TOOLS,
    _WATCH_TOOLS,
    build_subagent_prompt,
    build_system_prompt,
    build_system_prompt_parts,
)
from graph.subagents.config import SUBAGENT_REGISTRY

# ── ceilings (chars) — mirror docs/explanation/prompt-contracts.md ───────────────────

LEAD_DOCTRINE_MAX = 7_700  # Subagents + Operating model + Guidelines, all capabilities bound
LEAD_SUBAGENTS_MAX = 3_700
LEAD_OPERATING_MODEL_MAX = 3_050
LEAD_GUIDELINES_MAX = 1_050
LEAD_MINIMAL_MAX = 500  # no subagents, no autonomous primitives
SUBAGENT_PROMPT_MAX = 8_500
LEAD_VISIBLE_SUBAGENT_PROMPT_MAX = 4_000

_RAISE_HINT = (
    "raise the ceiling in tests/test_prompt_budgets.py only with a "
    "docs/explanation/prompt-contracts.md update explaining why"
)

CAPABILITY_GROUPS = {
    "goal": _GOAL_TOOLS,
    "tasks": _TASK_TOOLS,
    "schedule": _SCHEDULE_TOOLS,
    "watch": _WATCH_TOOLS,
    "wait": _WAIT_TOOLS,
}
ALL_CAPABILITY_TOOLS = frozenset().union(*CAPABILITY_GROUPS.values())
# Every autonomous primitive plus the two tools the guidelines gate on.
ALL_BOUND = ALL_CAPABILITY_TOOLS | {"task", "current_time"}
FILLER_ONLY = frozenset({"current_time"})


@pytest.fixture(autouse=True)
def _fixture_soul_and_no_delegates(monkeypatch, tmp_path: Path):
    """A deterministic prompt: tiny fixture SOUL, no delegate registry."""
    soul = tmp_path / "config" / "SOUL.md"
    soul.parent.mkdir(parents=True)
    soul.write_text("# Identity\nFixture persona for prompt-budget measurement.\n", encoding="utf-8")
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "delegate_registry", None, raising=False)


def _sections(**kw) -> dict[str, str]:
    parts = build_system_prompt_parts(**kw)
    labels = [label for label, _ in parts]
    assert len(labels) == len(set(labels)), f"duplicate prompt sections: {labels}"
    return dict(parts)


def _assert_under(name: str, actual: int, ceiling: int) -> None:
    assert actual <= ceiling, (
        f"{name} is {actual:,} chars (~{actual // 4:,} tokens); ceiling {ceiling:,} — {_RAISE_HINT}"
    )


# ── lead agent ───────────────────────────────────────────────────────────────────────


def test_lead_doctrine_within_budget():
    """Subagents + operating model + guidelines, every capability bound."""
    s = _sections(include_subagents=True, bound_tool_names=ALL_BOUND)
    assert set(s) == {"SOUL", "Subagents", "Operating model", "Guidelines"}, sorted(s)
    doctrine = sum(len(t) for label, t in s.items() if label != "SOUL")
    _assert_under("lead doctrine (ex-SOUL)", doctrine, LEAD_DOCTRINE_MAX)
    _assert_under("lead Subagents section", len(s["Subagents"]), LEAD_SUBAGENTS_MAX)
    _assert_under("lead Operating model section", len(s["Operating model"]), LEAD_OPERATING_MODEL_MAX)
    _assert_under("lead Guidelines section", len(s["Guidelines"]), LEAD_GUIDELINES_MAX)


def test_legacy_none_is_not_larger_than_all_bound():
    """``bound_tool_names=None`` (legacy) emits everything — and nothing more."""
    legacy = _sections(include_subagents=True, bound_tool_names=None)
    full = _sections(include_subagents=True, bound_tool_names=ALL_BOUND)
    assert {k: len(v) for k, v in legacy.items()} == {k: len(v) for k, v in full.items()}


def test_lead_minimal_within_budget():
    """A stripped deployment: no roster, no autonomous primitives → guidelines only."""
    s = _sections(include_subagents=False, bound_tool_names=FILLER_ONLY)
    assert set(s) == {"SOUL", "Guidelines"}, sorted(s)
    _assert_under("lead minimal (ex-SOUL)", len(s["Guidelines"]), LEAD_MINIMAL_MAX)


def test_build_system_prompt_is_parts_joined():
    parts = build_system_prompt_parts(include_subagents=True, bound_tool_names=ALL_BOUND)
    assert build_system_prompt(include_subagents=True, bound_tool_names=ALL_BOUND) == "\n\n".join(
        t for _, t in parts
    )


# ── honesty: capability-derived doctrine ─────────────────────────────────────────────


def test_roster_lists_only_lead_visible_subagents():
    roster = _sections(include_subagents=True, bound_tool_names=ALL_BOUND)["Subagents"]
    for name, cfg in SUBAGENT_REGISTRY.items():
        listed = f"**{name}**" in roster
        assert listed == cfg.lead_visible, (
            f"{name}: lead_visible={cfg.lead_visible} but {'listed' if listed else 'absent'} in the roster"
        )


def test_empty_bound_set_has_no_operating_model():
    s = _sections(include_subagents=False, bound_tool_names=FILLER_ONLY)
    assert "Operating model" not in s
    prompt = "\n\n".join(s.values())
    for tool in ALL_CAPABILITY_TOOLS:
        assert tool not in prompt, f"unbound tool {tool!r} is mentioned in a minimal prompt"


@pytest.mark.parametrize("group", sorted(CAPABILITY_GROUPS))
def test_binding_one_group_never_names_another_groups_tools(group):
    """Bind exactly one capability group: the prompt must not mention any other group's
    tools (the honesty invariant the capability map exists for)."""
    bound = CAPABILITY_GROUPS[group] | FILLER_ONLY
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=bound)
    for other, tools in CAPABILITY_GROUPS.items():
        if other == group:
            continue
        for tool in tools:
            # `wait` is also an English word inside the doctrine; match the tool form.
            needle = f"`{tool}`" if tool == "wait" else tool
            assert needle not in prompt, f"bound only {group!r}, but {tool!r} ({other}) is mentioned"


# ── subagents ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(SUBAGENT_REGISTRY))
def test_subagent_prompt_within_budget(name):
    prompt = build_subagent_prompt(name)
    assert prompt == SUBAGENT_REGISTRY[name].system_prompt  # verbatim: no SOUL, no roster
    _assert_under(f"subagent prompt {name!r}", len(prompt), SUBAGENT_PROMPT_MAX)
    if SUBAGENT_REGISTRY[name].lead_visible:
        _assert_under(f"lead-visible subagent prompt {name!r}", len(prompt), LEAD_VISIBLE_SUBAGENT_PROMPT_MAX)


def test_unknown_subagent_gets_generic_prompt():
    assert "subagent" in build_subagent_prompt("no-such-subagent").lower()


# ── external / ACP runtime ───────────────────────────────────────────────────────────


def test_acp_stable_prefix_equals_lead_prompt():
    """``build_stable_prefix`` delegates — same inputs, byte-equal output (ADR 0108 D8)."""
    from runtime.context import build_stable_prefix

    for include_subagents, bound in ((True, ALL_BOUND), (False, FILLER_ONLY), (True, None)):
        assert build_stable_prefix(include_subagents=include_subagents, bound_tool_names=bound) == (
            build_system_prompt(include_subagents=include_subagents, bound_tool_names=bound)
        )


# ── provider-transformed ─────────────────────────────────────────────────────────────


class _Req:
    """The slice of ModelRequest the provider transforms touch."""

    def __init__(self, system_message, model_settings=None):
        self.system_message = system_message
        self.model_settings = model_settings
        self.model = object()

    def override(self, **changes):
        new = _Req(self.system_message, self.model_settings)
        for k, v in changes.items():
            setattr(new, k, v)
        return new


def _system_message(text: str):
    from langchain_core.messages import SystemMessage

    return SystemMessage(content=text)


def test_claude_code_identity_transform_prefixes_and_preserves_text():
    from graph.middleware.claude_code_identity import ClaudeCodeIdentityMiddleware
    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    text = build_system_prompt(include_subagents=True, bound_tool_names=ALL_BOUND)
    out = ClaudeCodeIdentityMiddleware()._transform(_Req(_system_message(text)))
    blocks = out.system_message.content
    assert isinstance(blocks, list) and blocks[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    assert blocks[1]["text"] == text  # composed text untouched, just re-containered
    # Idempotent: re-running never stacks the prefix.
    again = ClaudeCodeIdentityMiddleware()._transform(out)
    assert again.system_message.content == blocks


def test_codex_responses_transform_moves_text_without_change():
    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    text = build_system_prompt(include_subagents=True, bound_tool_names=ALL_BOUND)
    out = CodexResponsesInputMiddleware()._transform(_Req(_system_message(text)))
    assert out.system_message is None
    assert out.model_settings["instructions"] == text
