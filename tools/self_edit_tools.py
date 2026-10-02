"""Self-editing tools — the agent's guarded writers over its own skills, persona and config.

- ``_build_curation_tools`` — ``recent_activity`` / ``list_skills`` / additive-only
  ``save_skill`` for the ``dream`` / ``distill`` curation subagents (ADR 0054).
- ``_build_skill_editor_tools`` — guarded ``update_skill`` / ``delete_skill``; each
  mutation archives the outgoing version for rollback.
- ``_build_soul_editor_tool`` — guarded ``edit_soul`` persona editor (ADR 0079/0081).
- ``_build_config_editor_tool`` — guarded ``set_config`` (``tools.self_config_enabled``),
  fenced by ``_CONFIG_WRITE_DENIED`` / ``_CONFIG_WRITE_DENIED_LEAVES``.
- ``_build_fleet_diagnostics_tool`` — the read-only ``fleet_diagnostics`` adapter
  (ADR 0071 / #3170).

Split out of ``tools/lg_tools.py`` (#3839, epic #3804). ``lg_tools`` re-exports every name
here so existing imports — tests, plugins, forks, ``runtime/operator_mcp_tools.py`` — keep
working; ``get_all_tools`` there is still the one assembly point.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tools.session import _session_id_from

log = logging.getLogger("protoagent.tools")  # same logger lg_tools used


def _build_curation_tools(*, provenance_required: bool = False):
    """Read-mostly tools for the memory/skill curation subagents (`dream` /
    `distill`, ADR 0054). They read from STATE at call time (the ``set_goal``
    pattern) so ``get_all_tools`` needs no new wiring, and they are deliberately
    scoped: two read-only surfaces over what the agent has actually been doing
    (``recent_activity``, ``list_skills``) plus one *additive-only* writer
    (``save_skill``). There is no raw-SQL / shell escape hatch — the whole class
    of "the consolidation agent rewrote the trajectory DB" risk simply can't
    happen here, unlike a bash-driven distill."""

    @tool
    def recent_activity(limit: int = 30, window_hours: int = 168) -> str:
        """Read-only digest of what the agent has recently DONE — for spotting
        repeated workflows or durable facts worth consolidating.

        Combines the Activity feed (recent assistant turns: time · origin ·
        trigger · text) with a telemetry rollup (turn/tool/cost volume, per
        model) over the last ``window_hours`` (default 7 days). Use this as the
        primary evidence source for `/dream` and `/distill`. Purely read-only —
        it never modifies anything.
        """
        from datetime import datetime, timedelta, timezone

        from runtime.state import STATE

        lim = max(1, min(int(limit or 30), 200))
        out: list[str] = []

        ts = STATE.telemetry_store
        if ts is not None:
            try:
                since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(window_hours or 168)))).isoformat()
                s = ts.summary(since_iso=since)
                out.append(
                    f"## Telemetry (last {window_hours}h): {s.get('turns', 0)} turns, "
                    f"{s.get('tool_calls', 0)} tool calls, {s.get('llm_calls', 0)} LLM calls, "
                    f"${s.get('cost_usd', 0.0):.4f}, success {s.get('success_rate', 0.0):.0%}"
                )
                by_model = s.get("by_model") or []
                if by_model:
                    out.append(
                        "By model: "
                        + "; ".join(
                            f"{m.get('model') or '?'} ×{m.get('turns', 0)} (${m.get('cost_usd', 0.0):.4f})"
                            for m in by_model[:8]
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                out.append(f"(telemetry rollup unavailable: {exc})")

        al = STATE.activity_log
        if al is not None:
            rows = al.recent(limit=lim)
            if rows:
                out.append(f"\n## Recent activity ({len(rows)} most recent turns):")
                for r in rows:
                    text = " ".join((r.get("text") or "").split())
                    if len(text) > 240:
                        text = text[:239] + "…"
                    tag = "/".join(p for p in (r.get("origin"), r.get("trigger")) if p)
                    out.append(f"- [{r.get('created_at', '')[:19]}] ({tag or 'operator'}) {text}")
        if not out:
            return (
                "No activity or telemetry is available yet — nothing to consolidate or distill. Report this and stop."
            )
        return "\n".join(out)

    @tool
    def list_skills() -> str:
        """List every skill already in the index (name · source · confidence ·
        description) so a distill/dream pass reuses or extends instead of
        duplicating. Read-only.
        """
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available."
        skills = idx.all_skills()
        if not skills:
            return "No skills are indexed yet."
        skills.sort(key=lambda s: (s.get("source") or "", -(s.get("confidence") or 0.0)))
        lines = [f"{len(skills)} skill(s) indexed:"]
        for s in skills:
            desc = " ".join((s.get("description") or "").split())
            if len(desc) > 120:
                desc = desc[:119] + "…"
            conf = s.get("confidence")
            conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
            lines.append(f"- {s.get('name')} [{s.get('source') or '?'} · conf {conf_s}] — {desc}")
        return "\n".join(lines)

    @tool
    def save_skill(
        name: str,
        description: str,
        body: str,
        tools: list[str] | None = None,
        provenance_reason: str = "",
        source_session_id: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Create a NEW reusable skill (a procedure/playbook the agent will be
        offered for matching tasks). ADDITIVE-ONLY — refuses if a skill with that
        name already exists (it never overwrites; to revise an existing skill,
        propose it for review instead). Use this only for high-confidence,
        clearly-missing workflows during a `/distill` pass.

        `name` is a short label, `description` a focused one-liner (what it does /
        when to use it), `body` the procedure prompt, `tools` the tool names it
        relies on. Saved as a curator-managed skill (confidence decays if it goes
        unused), so a mistaken capture self-cleans rather than accumulating.
        """
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available — cannot save."
        name = (name or "").strip()
        if not name:
            return "Error: skill name is required."
        if not (description or "").strip():
            return "Error: a one-line description is required (it's how the skill is matched)."
        from graph.skills.authoring import slugify

        all_skills = idx.all_skills()
        existing = {(s.get("name") or "").strip().lower() for s in all_skills}
        if name.lower() in existing:
            return (
                f"A skill named {name!r} already exists — refusing to overwrite "
                "(additive-only). Pick a distinct name, or propose extending the "
                "existing one for review instead of auto-creating."
            )
        target_slug = slugify(name)
        if not target_slug:
            return "Error: skill name must contain at least one ASCII letter or digit."
        if any(slugify(s.get("name", "")) == target_slug for s in all_skills):
            return f"Error: skill name {name!r} collides with an existing skill's storage path."
        from graph.extensions.skills import SkillV1Artifact

        trusted_session_id = _session_id_from(state)
        session_id = trusted_session_id if provenance_required else ((source_session_id or "").strip() or trusted_session_id)
        if provenance_required and (not session_id or not (provenance_reason or "").strip()):
            return "Error: self-improvement writes require a trusted source session and evidence-based provenance reason."
        prompt = body or ""
        clean_reason = " ".join(provenance_reason.split()).replace("--", "—")
        if clean_reason:
            prompt = (
                f"{prompt.rstrip()}\n\n<!-- self-improvement provenance: "
                f"session={session_id or 'unknown'}; reason={clean_reason} -->"
            )
        try:
            art = SkillV1Artifact(
                name=name,
                description=description.strip(),
                prompt_template=prompt,
                tools_used=list(tools or []),
                source_session_id=session_id,
            )
        except (ValueError, TypeError) as exc:
            return f"Error building skill: {exc}"
        inserted_id = idx.add_skill(art, source="distilled")
        if provenance_required and inserted_id is None:
            return "Error: the skills index did not confirm creation; no skill was reported as created."
        return (
            f"Created skill {name!r} (source=distilled, confidence 1.0). It'll be "
            "listed in the agent's <available_skills> index and loadable on demand "
            "via load_skill — curator-managed (confidence decays if unused)."
        )

    return [recent_activity, list_skills, save_skill]


def _build_skill_editor_tools(*, provenance_required: bool = False):
    """Guarded update/delete tools for ``self_improvement.skills: auto``.

    Every destructive mutation snapshots the outgoing skill under the instance's
    ``skills/.history`` tree before changing the live index/file.
    """

    @tool
    def update_skill(
        name: str,
        description: str,
        body: str,
        reason: str,
        tools: list[str] | None = None,
        source_session_id: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Update an editable reusable skill after learning a concrete improvement.

        Requires the complete replacement description/body plus an evidence-based
        reason. The outgoing version is archived for rollback and the replacement is
        stamped with the producing session. Bundled and commons skills are read-only.
        """
        from graph.skills.authoring import archive_skill, classify, restore_skill_snapshot, slugify, write_skill
        from infra.paths import user_skills_dir
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available — cannot update."
        if not all((name or "").strip() for name in (name, description, body, reason)):
            return "Error: name, description, body, and evidence-based reason are required."
        current = next((s for s in idx.all_skills() if str(s.get("name", "")).casefold() == name.strip().casefold()), None)
        if current is None:
            return f"No skill named {name!r} exists."
        root = user_skills_dir(create=True)
        target_slug = slugify(str(current.get("name", "")))
        if not target_slug:
            return "Error: refusing to update a skill with an empty storage slug."
        if any(
            s.get("id") != current.get("id") and slugify(str(s.get("name", ""))) == target_slug
            for s in idx.all_skills()
        ):
            return f"Error: refusing to update {name!r}; its storage path collides with another skill."
        origin, editable = classify(current, root)
        if not editable:
            return f"Refusing to update {origin} skill {name!r}; only user/learned skills are editable."
        trusted_session_id = _session_id_from(state)
        session_id = trusted_session_id if provenance_required else ((source_session_id or "").strip() or trusted_session_id)
        if provenance_required and not session_id:
            return "Error: self-improvement writes require a trusted source session."
        backup = archive_skill(root, current, session_id=session_id, reason=reason.strip())
        try:
            artifact = write_skill(
                root,
                current.get("name", name),
                description.strip(),
                body.strip(),
                tools=list(current.get("tools_used") or []) if tools is None else list(tools),
                user_facing=bool(current.get("user_facing", False)),
                slash=str(current.get("slash", "") or ""),
                user_only=bool(current.get("user_only", False)),
                provenance={"session_id": session_id, "reason": reason.strip()},
            )
            if not idx.delete_skill(int(current["id"])):
                restore_skill_snapshot(root, current.get("name", name), backup)
                return f"Error updating skill: the index did not confirm removal of the old row; restored {backup}."
            inserted_id = idx.add_skill(artifact, source="disk")
            if inserted_id is None:
                restored = restore_skill_snapshot(root, current.get("name", name), backup)
                idx.add_skill(restored, source="disk")
                return f"Error updating skill: the index did not confirm the replacement; restored {backup}."
        except Exception as exc:  # noqa: BLE001
            return f"Error updating skill (backup retained at {backup}): {exc}"
        return f"Updated skill {name!r}; previous version archived at {backup}."

    @tool
    def delete_skill(
        name: str,
        reason: str,
        source_session_id: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Delete an obsolete editable skill with an evidence-based reason.

        The complete outgoing skill is archived for rollback first. Bundled and
        commons skills are always read-only.
        """
        from graph.skills.authoring import archive_skill, classify, remove_skill, slugify
        from graph.skills.loader import parse_skill_md
        from infra.paths import user_skills_dir
        from runtime.state import STATE

        idx = STATE.skills_index
        if idx is None:
            return "Skills index is not available — cannot delete."
        if not (name or "").strip() or not (reason or "").strip():
            return "Error: name and evidence-based reason are required."
        current = next((s for s in idx.all_skills() if str(s.get("name", "")).casefold() == name.strip().casefold()), None)
        if current is None:
            return f"No skill named {name!r} exists."
        root = user_skills_dir(create=True)
        target_slug = slugify(str(current.get("name", "")))
        if not target_slug:
            return "Error: refusing to delete a skill with an empty storage slug."
        if any(
            s.get("id") != current.get("id") and slugify(str(s.get("name", ""))) == target_slug
            for s in idx.all_skills()
        ):
            return f"Error: refusing to delete {name!r}; its storage path collides with another skill."
        origin, editable = classify(current, root)
        if not editable:
            return f"Refusing to delete {origin} skill {name!r}; only user/learned skills are editable."
        trusted_session_id = _session_id_from(state)
        session_id = trusted_session_id if provenance_required else ((source_session_id or "").strip() or trusted_session_id)
        if provenance_required and not session_id:
            return "Error: self-improvement writes require a trusted source session."
        backup = archive_skill(root, current, session_id=session_id, reason=reason.strip())
        if not idx.delete_skill(int(current["id"])):
            return f"Error: the index did not confirm deletion of {name!r}; backup retained at {backup}."
        if origin == "user" and not remove_skill(root, current.get("name", name)):
            restored = parse_skill_md(backup)
            if restored is not None:
                idx.add_skill(restored, source="disk")
            return f"Error: could not remove live skill {name!r}; index restored and backup retained at {backup}."
        return f"Deleted skill {name!r}; previous version archived at {backup}."

    return [update_skill, delete_skill]


# ── self-authored persona: edit_soul (guarded, ADR 0079/0066/0081) ────────────

# Cap the whole persona so a self-edit can't grow SOUL.md unbounded — it rides in the
# system-prompt prefix on every turn (and the cached prefix), so it has to stay tight.
_SOUL_MAX_BYTES = 64 * 1024


def _apply_soul_section_edit(text: str, section: str, content: str, mode: str) -> str:
    """Return ``text`` (a SOUL.md) with markdown ``section`` replaced or appended.

    The heading is matched case-insensitively on its trimmed title at any level
    (``#``…``######``); the section body runs from just after that heading to the next
    heading of the *same or higher* level (or EOF). ``mode`` is ``"replace"`` (swap the
    body) or ``"append"`` (add to it). A section that doesn't exist yet is CREATED as a
    new ``## <section>`` block at the end. Whole-file scope is deliberately not offered —
    every edit is one section, so a single call can't blow away the persona.
    """
    import re

    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    target = section.strip().lower()
    body = content.strip()
    lines = text.splitlines()

    head_idx = level = None
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m and m.group(2).strip().lower() == target:
            head_idx, level = i, len(m.group(1))
            break

    if head_idx is None:
        # New section — append at the end, one blank line off the existing content.
        block = f"## {section.strip()}\n\n{body}\n" if body else f"## {section.strip()}\n"
        base = text.rstrip("\n")
        return f"{base}\n\n{block}" if base else block

    end = len(lines)
    for j in range(head_idx + 1, len(lines)):
        m = heading_re.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break

    existing = "\n".join(lines[head_idx + 1 : end]).strip("\n")
    if mode == "append":
        new_body = f"{existing}\n\n{body}".strip("\n") if existing else body
    else:  # replace
        new_body = body

    rebuilt = lines[: head_idx + 1]
    if new_body:
        rebuilt += ["", *new_body.splitlines(), ""]
    else:
        rebuilt += [""]
    rebuilt += lines[end:]
    return "\n".join(rebuilt).rstrip("\n") + "\n"


def _publish_persona_event(topic: str, data: dict) -> None:
    """Best-effort operator notice on the server→client event bus (ADR 0039) — so a persona
    self-edit is visible in the console even when it lands on an AUTONOMOUS turn (scheduled /
    activity) with no human watching the chat. This is the ADR 0081 transparency guardrail —
    identity changes are never silent — and mirrors OpenClaw's shipped "tell the user, it's
    your soul" convention. No-op when the host hasn't wired a publisher (unit tests /
    standalone use); a bus hiccup must never break the edit. Mirrors goals/watches store push."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass



# ── guarded agent-owned config writes (tools.self_config_enabled) ─────────────────────
# The trust surface an agent must never be able to widen from inside. These are the knobs
# that decide what the agent is ALLOWED to do — shell execution, which directories the
# filesystem tools can reach, egress, which plugins run in-process as the agent, and the
# credentials gating the operator API. A tool that could edit them is not a config tool,
# it is a privilege-escalation tool, so the fence is a denylist checked before anything
# reaches the write path (ADR 0071 / ADR 0089 draw exactly these lines).
_CONFIG_WRITE_DENIED = (
    "acp",               # ACP runtime specs carry a `command` the host spawns
    "auth",              # bearer/token config — the operator API's gate
    "delegates",         # each entry is an EXECUTABLE (command/args/workdir, permissions: auto)
    "egress",            # ADR 0008 network fence
    "filesystem",        # allow_run + the ADR 0007 project fence
    "mcp",               # out-of-process tool servers = new capability
    "operator",          # allowed_dirs / project_dir — the operator fence
    "plugins",           # enabling a plugin runs its code in-process AS the agent
    "runtime",           # per-agent command overrides, same spawn path as delegates
    "security",
    "self_improvement", # automatic durable mutation policy is operator-owned
    "soul",              # persona has its own guarded path (edit_soul, ADR 0081)
    "tools",             # incl. self_config_enabled itself: no self-widening
)

# Section denial can't be the whole story: plugin config sections are named after their
# plugin, so the fence can't enumerate them, and any plugin is free to grow a key naming a
# binary. So below the section the fence works on NAMES, in three layers:
#
# 1. a key segment that IS one of these words (``coder.command``);
# 2. a key segment with one of these words as a ``_``/``-``/camelCase TOKEN
#    (``local_gate_cmd``, ``proxy_command``, ``rh_bin``, ``binary_path``, ``browserArgs``),
#    which catches the conventional spellings without a list of every plugin's keys; and
# 3. a plugin setting its manifest marks ``spawns: true`` — for the names convention can't
#    see (``ffmpeg_path``). The marker is read from EVERY installed plugin, enabled or not,
#    so a value can't be planted while the plugin is off and spawned once the operator
#    turns it on; if discovery fails, plugin-section writes are refused (fail closed).
#
# A blanket ``*_path`` rule was considered and rejected: most ``*_path`` settings name DATA
# (``brand_kit_path`` in two plugins), which an agent should keep being able to repoint. An
# agent may CHOOSE among the executables its operator provisioned
# (``project_board.coder: proto``) but never DEFINE one.
_CONFIG_WRITE_DENIED_LEAVES = frozenset(
    {"args", "argv", "bin", "binary", "cmd", "command", "entrypoint", "exe", "executable", "interpreter"}
)

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_MAX_WRITE_DEPTH = 32


def _key_tokens(segment: str) -> set[str]:
    """``localGate_CMD`` → ``{"local", "gate", "cmd"}`` — the words a key segment is built from."""
    return {t for t in _TOKEN_SPLIT.split(_CAMEL_BOUNDARY.sub(r"\1_\2", segment).lower()) if t}


def _write_paths(updates: dict) -> list[str]:
    """Every dotted path a write would touch — including keys hidden INSIDE a dict or list
    value. ``{"campaign": {"ffmpeg_path": "/bin/sh"}}`` writes ``campaign.ffmpeg_path`` just
    as surely as the flat spelling does (``nest_updates`` keeps the dict), so the fence has
    to see through it or the nested spelling is a bypass. A payload nested past
    ``_MAX_WRITE_DEPTH`` yields a ``<too-deep>`` marker the caller refuses."""
    out: list[str] = []

    def walk(prefix: str, value: Any, depth: int) -> None:
        out.append(prefix)
        if depth > _MAX_WRITE_DEPTH:
            out.append(f"{prefix}.<too-deep>")
            return
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{str(k).strip()}", v, depth + 1)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                if isinstance(v, (dict, list, tuple)):
                    walk(f"{prefix}.{i}", v, depth + 1)

    for key, value in updates.items():
        walk(".".join(part.strip() for part in str(key).split(".")), value, 0)
    return out


def _names_a_program(path: str) -> bool:
    """True when any segment BELOW the section names a program to run (layers 1 + 2)."""
    for segment in path.split(".")[1:]:
        if segment.lower() in _CONFIG_WRITE_DENIED_LEAVES or _key_tokens(segment) & _CONFIG_WRITE_DENIED_LEAVES:
            return True
    return False


def _core_sections() -> set[str]:
    """Top-level sections core itself owns — writes there never need plugin discovery."""
    from graph.settings_schema import FIELDS, SETTINGS_EXEMPT_SECTIONS

    return {f.key.split(".", 1)[0] for f in FIELDS} | set(SETTINGS_EXEMPT_SECTIONS)


def _spawn_marked_keys() -> set[str]:
    """``<section>.<key>`` (lowercased) for every plugin setting marked ``spawns: true``,
    across every INSTALLED plugin. Raises when discovery fails — the caller fails closed."""
    from graph.plugins.pconfig import installed_plugin_config_schemas

    marked: set[str] = set()
    for sch in installed_plugin_config_schemas(strict=True):
        for spec in sch.settings or []:
            if isinstance(spec, dict) and spec.get("key") and spec.get("spawns"):
                marked.add(f"{sch.section}.{spec['key']}".lower())
    return marked


def _config_write_refusal(updates: dict) -> str | None:
    """The reason this write is refused, or None when every key is inside the fence.

    Checks the TOP-LEVEL section of each dotted key. Deliberately coarse: a denied section
    is denied entirely, because "which sub-key of `filesystem` is safe" is exactly the
    judgement call that shouldn't live in a tool the agent can call. Below the section,
    every path the write touches — nested values included — is checked for a name that
    defines a program to run.
    """
    paths = _write_paths(updates)
    denied = sorted({p.split(".", 1)[0] for p in paths if p.split(".", 1)[0] in _CONFIG_WRITE_DENIED})
    if denied:
        return (
            f"Refused: {', '.join(denied)} is outside what you may change. Those settings decide what "
            f"you are allowed to do (shell access, reachable directories, egress, which plugins run as "
            f"you, the executables that get spawned, operator credentials) — only your operator can "
            f"change them, from Settings. Everything else (models, routing, plugin behavior) is yours."
        )
    executable = {p for p in paths if p.endswith(".<too-deep>") or _names_a_program(p)}

    # Layer 3, the manifest marker. Only a plugin section can carry one, so a pure core
    # write never pays for discovery.
    try:
        core = _core_sections()
    except Exception:  # noqa: BLE001 — schema unavailable: treat every section as a plugin's
        core = set()
    plugin_paths = [p for p in paths if p.split(".", 1)[0] not in core]
    if plugin_paths and not executable:
        try:
            marked = _spawn_marked_keys()
        except Exception:  # noqa: BLE001 — can't tell which settings spawn: refuse, don't guess
            log.warning("[set_config] plugin settings discovery failed; refusing the plugin-section write", exc_info=True)
            sections = ", ".join(sorted({p.split(".", 1)[0] for p in plugin_paths}))
            return (
                f"Refused: couldn't verify which {sections} settings name a program to run, so "
                f"nothing was applied. Try again, or ask your operator to make the change."
            )
        for p in plugin_paths:
            low = p.lower()
            if any(low == m or low.startswith(m + ".") for m in marked):
                executable.add(p)

    if executable:
        # Name the keys as the agent wrote them, not every nested path under them.
        shown = sorted(
            {k for k in updates if any(p == k or p.startswith(f"{k}.") for p in executable)} or executable
        )
        return (
            f"Refused: {', '.join(shown)} names a program to run. You may choose among the "
            f"executables your operator has already provisioned (e.g. a coder by name), but defining "
            f"one is theirs to do."
        )
    return None


def _build_config_editor_tool() -> list:
    """Bind the guarded config editor (config ``tools.self_config_enabled``, default off).

    A fourth adapter over ``ops.config.set_config`` — the same op behind ``protoagent config
    set`` and the console's ``PATCH /api/config`` (ADR 0075 D2: one op, thin adapters). It
    reaches the live write path through ``HOST.apply_settings`` because ``tools/`` must never
    import ``server/`` (the import-layering contract), the same reason ``onboard_project``
    does. Lead-agent only: no subagent build passes the flag.
    """

    @tool
    async def set_config(updates: dict) -> str:
        """Change your own operational configuration — models, routing, and plugin settings.

        Use this when you need to retune how you run: point a plugin at a different coder,
        change a model slot, adjust a plugin's behavior. The change is persisted and applied
        immediately (a reload), and your operator is notified.

        - ``updates``: a flat dict of dotted config keys → values, e.g.
          ``{"project_board.coder": "proto", "routing.aux_model": "claude-opus-4-6"}``.

        SCOPE — operational settings only. You cannot change what you are ALLOWED to do:
        shell execution, reachable directories, egress, which plugins run, operator
        credentials, or your own persona (that's `edit_soul`). Those are your operator's,
        and attempting them returns a refusal rather than a partial write. Secrets are never
        accepted here — ask your operator to set those in Settings.

        Returns a confirmation naming what changed, or a readable error. Prefer one call
        with all the related keys: each call reloads the agent."""
        if not isinstance(updates, dict) or not updates:
            return "Error: 'updates' must be a non-empty dict of dotted config keys, e.g. {'project_board.coder': 'proto'}."

        flat = {str(k).strip(): v for k, v in updates.items() if str(k).strip()}
        if not flat:
            return "Error: no usable keys in 'updates'."

        refusal = _config_write_refusal(flat)
        if refusal:
            log.warning("[set_config] refused out-of-fence write: %s", sorted(flat))
            return refusal

        # Secrets never travel through the agent. The op would faithfully route a
        # secret-typed key into secrets.yaml; that is right for the CLI and the console and
        # wrong here, because it would put a live credential in the turn transcript.
        try:
            from graph.settings_schema import _SECRET_KEYS

            secrets = sorted(p for p in _write_paths(flat) if p in _SECRET_KEYS)  # nested values too
        except Exception:  # noqa: BLE001 — schema unavailable (standalone/tests): fall through
            secrets = []
        if secrets:
            return (
                f"Refused: {', '.join(secrets)} is a secret. Credentials are never set through me — "
                f"ask your operator to enter it in Settings."
            )

        from graph.plugins.host import HOST

        if HOST.apply_settings is None:
            return "Error: config writes are unavailable — the host is not wired for config apply (no running server)."

        from graph.settings_schema import nest_updates

        try:
            ok, messages = await asyncio.to_thread(HOST.apply_settings, nest_updates(flat))
        except Exception as e:  # noqa: BLE001 — a bad value must not kill the turn
            log.exception("[set_config] apply failed")
            return f"Error: could not apply the change: {e}"

        detail = "; ".join(messages or [])
        if not ok:
            return f"Error: the change was rejected: {detail or 'unknown error'}"

        changed = ", ".join(f"{k}={v!r}" for k, v in sorted(flat.items()))
        _publish_persona_event("config.self_edited", {"keys": sorted(flat), "by": "agent"})
        log.info("[set_config] applied %s", changed)
        return f"Applied: {changed}. The change is live{(' — ' + detail) if detail else ''}."

    return [set_config]


def _build_fleet_diagnostics_tool() -> list:
    """Bind the read-only fleet diagnostics tool (``tools.fleet_diagnostics.enabled``).

    The core implementation resolves members only through the configured fleet roster and
    reads through the hub-owned diagnostics proxy. Keep this adapter narrow: it exposes logs
    and exact task-by-id reads only, with no runtime control, checkpoint, HITL, or config path.
    """

    @tool
    async def fleet_diagnostics(member: str, read: str = "logs", lines: int = 200, task_id: str = "") -> dict:
        """Read diagnostics from a registered fleet member.

        Use this only for inspection. It is read-only and cannot start members, resume or
        answer tasks, mutate checkpoints, or change configuration.

        Args:
            member: Fleet member display name or id from the configured roster.
            read: ``"logs"`` for recent logs, or ``"task"`` for one exact task.
            lines: Recent log line count for ``read="logs"``; clamped by the diagnostics layer.
            task_id: Exact A2A task id for ``read="task"``.

        Returns a compact dict with either diagnostics data or a structured refusal/error.
        """
        from tools.fleet_diagnostics import read_member_logs, read_member_task

        mode = (read or "logs").strip().lower()
        if mode in {"logs", "log"}:
            return await read_member_logs(member, lines)
        if mode in {"task", "tasks"}:
            return await read_member_task(member, task_id)
        detail = (
            "fleet_diagnostics only supports read='logs' or read='task'. "
            "It has no runtime control actions."
        )
        return {
            "ok": False,
            "error": "unsupported_read",
            "detail": detail,
            "member": (member or "").strip(),
        }

    return [fleet_diagnostics]


def _build_soul_editor_tool(reload_callback=None, *, provenance_required: bool = False) -> list:
    """Bind the guarded self-persona editor (config ``soul.self_edit_enabled``, default off).

    ``edit_soul`` lets the LEAD agent rewrite a section of its own ``SOUL.md`` — its identity
    and voice, NOT operating doctrine (ADR 0079). Every edit is snapshotted to soul-history
    (#1691, reversible) and takes effect on the agent's NEXT turn via a graph reload
    (``reload_callback``, injected by the server so ``tools/`` never imports ``server/``).
    Absent a callback (subagent/eval/test builds) the save still lands and applies on the
    next natural reload/restart."""

    @tool
    async def edit_soul(
        section: str,
        content: str,
        mode: str = "replace",
        reason: str = "",
        source_session_id: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Rewrite a section of your own persona file (SOUL.md) — how you think, speak, and carry
        yourself. Use this to durably refine your identity when you learn something about how you
        should show up.

        - ``section``: the markdown heading to edit, e.g. "Voice" or "Personality" (matched
          case-insensitively). If it doesn't exist yet, a new ``## <section>`` block is created.
        - ``content``: the new markdown for that section's body.
        - ``mode``: "replace" (swap the section body) or "append" (add to it).
        - ``reason``: optional evidence for why this durable persona change was warranted.

        SCOPE — persona ONLY: identity, values, voice, temperament. Do NOT put operating
        instructions, task doctrine, or tool rules here — SOUL.md stays pure persona (ADR 0079).
        Every edit is snapshotted and reversible from Settings ▸ Identity, your operator is
        notified of the change, and it takes effect on your next turn (not the current one).
        Returns a confirmation with the live persona revision."""
        from graph.config_io import read_soul, record_soul_edit_provenance, soul_revision, write_soul

        section = (section or "").strip()
        if not section:
            return "Error: 'section' is required — the heading to edit, e.g. 'Voice'."
        mode = (mode or "replace").strip().lower()
        if mode not in ("replace", "append"):
            return f"Error: mode must be 'replace' or 'append', got {mode!r}."
        content = content or ""
        if not content.strip():
            return (
                f"Error: refusing to write an empty section {section!r} — pass non-empty content. "
                "(To retire a persona trait, replace the section with its revised text instead.)"
            )
        trusted_session_id = _session_id_from(state)
        session_id = trusted_session_id if provenance_required else ((source_session_id or "").strip() or trusted_session_id)
        clean_reason = (reason or "").strip()
        if provenance_required and (not session_id or not clean_reason):
            return "Error: self-improvement persona writes require a trusted source session and evidence-based reason."

        try:
            current = read_soul()
        except Exception as exc:  # noqa: BLE001 — surface as a tool error string, never raise into the turn
            return f"Error: could not read the current persona: {exc}"

        try:
            updated = _apply_soul_section_edit(current, section, content, mode)
        except Exception as exc:  # noqa: BLE001
            return f"Error: could not apply the edit: {exc}"

        if updated == current:
            return f"No change: section {section!r} already matches that content."
        if len(updated.encode("utf-8")) > _SOUL_MAX_BYTES:
            return (
                f"Error: that edit would grow SOUL.md past the {_SOUL_MAX_BYTES // 1024} KB persona cap. "
                "Keep the persona tight — trim the section or fold it into an existing one."
            )

        new_rev = hashlib.sha1(updated.encode("utf-8")).hexdigest()[:8]
        provenance_path = None
        if provenance_required:
            try:
                provenance_path = record_soul_edit_provenance(
                    revision=new_rev,
                    session_id=session_id,
                    reason=clean_reason,
                    section=section,
                    mode=mode,
                )
            except Exception as exc:  # noqa: BLE001 — provenance is mandatory for this write path
                return f"Error: durable persona provenance could not be recorded; SOUL.md was not changed: {exc}"

        try:
            # Archives the OUTGOING persona to soul-history (#1691) before overwriting, so this
            # is reversible from Settings ▸ Identity.
            write_soul(updated)
        except Exception as exc:  # noqa: BLE001
            if provenance_path is not None:
                provenance_path.unlink(missing_ok=True)
            return f"Error: persona write failed: {exc}"

        new_rev = soul_revision()
        verb = "Replaced" if mode == "replace" else "Appended to"

        # Operator-visible notice (ADR 0081 transparency guardrail): every self-edit — including
        # one on an autonomous turn — surfaces in the console over the event bus, so an identity
        # change is never silent (also the trail if a prompt-injection ever drove one). Best-effort.
        _publish_persona_event(
            "persona.self_edited",
            {
                "section": section,
                "mode": mode,
                "revision": new_rev,
                "session_id": session_id,
                "reason": clean_reason,
                "summary": f"Agent edited its own persona — {verb.lower()} section '{section}' (SOUL.md → {new_rev}).",
            },
        )

        applied = "It will apply on the next reload/restart."
        if reload_callback is not None:
            try:
                # Heavy + synchronous (rebuilds the compiled graph); offload so we don't block the
                # event loop. Rebinding STATE.graph is atomic — the current turn keeps the old
                # persona, the NEXT turn gets the new one.
                result = await asyncio.to_thread(reload_callback)
                ok = result[0] if isinstance(result, tuple) else bool(result)
                applied = (
                    "It is now live for your next turn."
                    if ok
                    else "Saved, but the live reload reported a problem — it will apply on the next restart."
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[edit_soul] reload after persona edit failed: %s", exc)
                applied = "Saved, but the live reload failed — it will apply on the next restart."

        return (
            f"{verb} section '{section}' in SOUL.md — persona now at revision {new_rev}. "
            f"Your operator has been notified of the change. "
            f"The previous version is archived to soul-history and restorable from Settings ▸ Identity. "
            f"{applied}"
        )

    return [edit_soul]
