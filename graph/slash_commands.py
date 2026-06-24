"""Single source of truth for slash-command resolution (precedence + palette).

The chat dispatcher (``server.chat``) and the console command palette
(``operator_api.console_handlers``) both need to agree on what a ``/<token>``
does. They used to encode the ``workflow > subagent > skill`` precedence (and the
shadowed-skill rule) separately, which is how a shipped skill could be silently
unreachable. This module holds that logic ONCE.

It lives in ``graph/`` deliberately: ``operator_api`` must not import ``server``
(import-linter contract), so the shared code can't live in ``server.chat``. Both
layers may import ``graph``. It depends only on ``runtime.state`` (for the live
registries) and ``graph.subagents`` — never on ``server`` / ``operator_api``.
"""

from __future__ import annotations

import logging
import re

from runtime.state import STATE

log = logging.getLogger("protoagent.server")

# Slash tokens already warned as shadowed (a skill whose token a workflow/subagent
# claims) — warn once, not on every resolve.
_warned_shadowed_skills: set[str] = set()


def slugify_slash(raw: str) -> str:
    """Lowercase + non-alphanumerics→hyphens slug for a slash token."""
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")


def find_user_facing_skill(name: str):
    """The user-facing skill whose slash token matches ``/<name>``, or ``None``.
    Token = explicit ``slash:`` (lowercased) else a slug of the skill name."""
    token = slugify_slash(name)
    if not token:
        return None
    reader = getattr(STATE.skills_index, "user_facing_skills", None) if STATE.skills_index else None
    if reader is None:
        return None
    try:
        skills = reader()
    except Exception:
        return None
    for skill in skills:
        sk_token = (skill.get("slash") or "").strip().lower() or slugify_slash(skill.get("name", ""))
        if sk_token == token:
            return skill
    return None


def find_plugin_chat_command(name: str):
    """The plugin-registered chat command handler whose token matches ``/<name>``,
    or ``None``. Tokens are slugified+lowercased at registration, so we match the
    lowercased name (exact) and its slug (so ``/Issue`` and ``/foo_bar`` resolve a
    ``foo-bar`` token). User-only control commands (``register_chat_command``)."""
    commands = getattr(STATE, "plugin_chat_commands", None) or {}
    if not name or not commands:
        return None
    return commands.get(name.strip().lower()) or commands.get(slugify_slash(name))


async def run_plugin_chat_command(name: str, rest: str, session_id: str) -> str | None:
    """Invoke the plugin chat command matching ``/<name>`` and return its reply (the
    dispatcher short-circuits the turn with it), or ``None`` to fall through. A
    handler that itself returns ``None`` falls through too (it decided not to handle
    the message); precedence still excludes a same-named workflow/skill from firing
    because ``slash_kind`` reports ``plugin_command`` for the token. A raising handler
    is logged + swallowed into a ``⚠️`` reply so one bad plugin can't 500 the turn."""
    handler = find_plugin_chat_command(name)
    if handler is None:
        return None
    try:
        return await handler(rest, session_id)
    except Exception as exc:  # noqa: BLE001 — a bad plugin command must not break the turn
        log.warning("[slash] plugin chat command /%s failed: %s", name, exc)
        return f"⚠️ /{name} failed: {exc}"


def slash_kind(name: str) -> str | None:
    """The kind a ``/<name>`` slash command resolves to — the SINGLE source of
    precedence shared by the chat dispatcher and the console palette, so they can
    never disagree about what a token does. Reserved: ``goal``. Precedence:
    goal > plugin chat command > workflow > subagent > user-facing skill. Returns
    ``None`` for an unknown token. (Plugin commands/workflows/subagents match the
    bare name or its slug; skills match a slug.) ``/issue`` is no longer core — the
    github plugin owns it, so it resolves as a ``plugin_command``."""
    if not name:
        return None
    if name == "goal" or slugify_slash(name) == "goal":
        return "goal"
    if find_plugin_chat_command(name) is not None:
        return "plugin_command"
    if STATE.workflow_registry is not None and STATE.workflow_registry.get(name) is not None:
        return "workflow"
    try:
        from graph.subagents.config import SUBAGENT_REGISTRY

        if name in SUBAGENT_REGISTRY:
            return "subagent"
    except Exception:
        pass
    if find_user_facing_skill(name) is not None:
        return "skill"
    return None


def resolve_slash_commands() -> list[dict]:
    """Single source of truth for the slash-command inventory — every registered
    ``/<token>`` with its ``kind`` + display metadata, precedence applied via
    ``slash_kind`` (so the palette can't drift from the dispatcher). A skill
    shadowed by a workflow/subagent is excluded and warned once. ``goal`` is a
    control command surfaced separately by the caller."""
    cmds: list[dict] = []
    seen: set[str] = set()

    def _add(name, kind, description, usage):
        if not name or name in seen:
            return
        seen.add(name)
        cmds.append({"name": name, "kind": kind, "description": description, "usage": usage})

    # Plugin chat commands first — they sit just below ``goal`` in precedence, so a
    # workflow/skill of the same token must not shadow them in the palette.
    for token, handler in (getattr(STATE, "plugin_chat_commands", None) or {}).items():
        doc = (getattr(handler, "__doc__", "") or "").strip()
        desc = doc.splitlines()[0] if doc else f"Run the /{token} command."
        _add(token, "plugin_command", desc, f"/{token} …")

    if STATE.workflow_registry is not None:
        for wf in STATE.workflow_registry.list():
            if slash_kind(wf["name"]) != "workflow":  # a goal/plugin-command/issue of the same token wins
                continue
            declared = wf.get("inputs", []) or []
            req = "".join(f" <{i['name']}>" for i in declared if i.get("required"))
            opt = "".join(f" [{i['name']}]" for i in declared if not i.get("required"))
            _add(
                wf["name"],
                "workflow",
                wf.get("description") or f"Run the {wf['name']} workflow.",
                f"/{wf['name']}{req}{opt}",
            )

    try:
        from graph.subagents.config import SUBAGENT_REGISTRY
    except Exception:
        SUBAGENT_REGISTRY = {}
    for sname, cfg in SUBAGENT_REGISTRY.items():
        if slash_kind(sname) != "subagent":  # a workflow of the same name wins
            continue
        _add(sname, "subagent", getattr(cfg, "description", "") or f"Run the {sname} subagent.", f"/{sname} <prompt>")

    reader = getattr(STATE.skills_index, "user_facing_skills", None) if STATE.skills_index else None
    if reader is not None:
        try:
            ufs = reader()
        except Exception:
            ufs = []
        for skill in ufs:
            token = (skill.get("slash") or "").strip().lower() or slugify_slash(skill.get("name") or "")
            if not token or token == "goal":
                continue
            kind = slash_kind(token)
            if kind != "skill":
                if kind is not None and token not in _warned_shadowed_skills:
                    _warned_shadowed_skills.add(token)
                    log.warning(
                        "[skills] user-facing skill %r is unreachable: /%s is already "
                        "claimed by a %s (which wins dispatch). Rename the skill's `slash:`.",
                        skill.get("name") or token,
                        token,
                        kind,
                    )
                continue
            _add(token, "skill", skill.get("description") or f"Run the {token} skill.", f"/{token} [input]")
    return cmds
