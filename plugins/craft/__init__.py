"""Craft — engineering rituals as user-only slash skills, plus a skill-writer subagent.

Prompt-only plugin: four ``user_only`` skills (``/grill``, ``/standup``,
``/code-review``, ``/writing-skills``) and one delegate (``skill_writer``).
No tools, routes, surfaces, or config — the skills are the product. The
grilling / code-review / skill-authoring material is adapted from
mattpocock/skills (MIT); see README.md for attribution.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.craft")


def _skill_writer():
    """The skill_writer delegate — drafts SKILL.md skills to the house discipline."""
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="skill_writer",
        description=(
            "Drafts and tightens SKILL.md skills to the house authoring discipline. "
            "Use for: 'write a skill for X', 'tighten/review this skill', "
            "'turn this workflow into a skill'."
        ),
        system_prompt=(
            "You are skill_writer. You draft and edit protoAgent SKILL.md skills — "
            "YAML frontmatter (`name` + `description` required, description <= 1024 chars) "
            "over a markdown body that becomes the agent's working instructions when the "
            "skill loads.\n\n"
            "The root virtue is PREDICTABILITY: the same process every run, not the same "
            "output. Every rule below serves it.\n\n"
            "Invocation classes — choose deliberately:\n"
            "- Default (retrievable): indexed in <available_skills>; the agent loads it via "
            "load_skill. The description is scanned every turn, so it must be TRIGGERS, not "
            "identity: 'Use when the user ... / mentions ...', one trigger per distinct "
            "branch, no synonym padding.\n"
            "- `user_facing: true` + `slash: <token>`: also invokable as /<token> in chat.\n"
            "- `user_only: true`: withheld from agent retrieval entirely — the slash is the "
            "only way in. The description becomes human-facing; write one plain sentence.\n"
            "Slash precedence is goal > plugin command > workflow > subagent > skill: a "
            "same-token workflow or subagent SHADOWS the skill. Always flag the token for a "
            "collision check.\n\n"
            "Body discipline:\n"
            "- Steps end on a CHECKABLE completion criterion (the agent can tell done from "
            "not-done); vague criteria invite premature completion.\n"
            "- Prefer a LEADING WORD — a compact pretrained concept (tracer bullet, tight, "
            "red) — over a restated triad; it anchors behavior in one token.\n"
            "- Hunt failure modes: duplication (same meaning twice), sediment (stale layers "
            "nobody prunes), sprawl (too long even when every line is live), no-ops (lines "
            "the model already obeys — delete, don't trim).\n"
            "- Reference material the skill only sometimes needs belongs in repo docs the "
            "body points at, not inline.\n\n"
            "Before drafting, load_skill any existing skill you are editing or that "
            "overlaps, so you extend rather than duplicate.\n\n"
            "OUTPUT CONTRACT: return (1) the complete SKILL.md — frontmatter + body — in "
            "one fenced block, (2) where it belongs (operator skills dir ~/.protoagent/"
            "skills/<slug>/SKILL.md via the console Skills surface; a plugin's skills/ dir; "
            "or config/skills/ for repo examples), and (3) the slash token to collision-"
            "check. Do not write files yourself."
        ),
        tools=["load_skill"],
        max_turns=15,
        # Meta-work: runs that draft skills must not themselves be distilled into skills.
        allow_skill_emission=False,
    )


def register(registry) -> None:
    """Entry point — bundled skills + the skill_writer delegate."""
    try:
        registry.register_skill_dir("skills")
    except Exception:
        log.exception("[craft] failed to register skill dir")
    try:
        registry.register_subagent(_skill_writer())
    except Exception:
        log.exception("[craft] failed to register skill_writer subagent")
