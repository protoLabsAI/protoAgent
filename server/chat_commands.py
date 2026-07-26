"""Slash-command parsing + execution for the chat turn loop.

Extracted from ``server/chat.py`` (which was itself extracted from
``server/__init__.py`` in ADR 0023 phase 2). ``chat.py``'s docstring enumerated four
concerns and this was the one that isn't the turn loop: given a ``/name rest``
message, decide whether it names a workflow, a subagent or a user-facing skill, and
run it. The turn drivers call in through six entry points and are otherwise
uninterested in how a slash command is parsed.

Precedence between the three kinds is decided ONCE, by ``graph.slash_commands``'s
``slash_kind`` — a neutral module the console palette imports too, so the dispatcher
and the palette can't drift.

``server/chat.py`` re-imports these names, so ``server.<symbol>`` keeps resolving for
the ADR 0023 re-export block in ``server/__init__.py`` and for the test suite
(``tests/test_skill_slash.py`` imports ``server._parse_skill_command``).
"""

from graph.output_format import extract_output
from runtime.state import STATE


# --- Workflow slash commands (ADR 0002) --------------------------------------
# A chat message like ``/research-and-brief quantum computing`` runs the named
# workflow instead of a normal model turn — the slash-command analogue of the
# run_workflow tool. Free text maps to the first unset (required) input; explicit
# ``key=value`` tokens set named inputs. Short-circuits the turn like /goal does.


def _parse_slash_command(message: str) -> tuple[str, str]:
    """Split ``/name rest`` → (name, rest). Returns ("", "") if not a slash msg."""
    s = (message or "").strip()
    if not s.startswith("/"):
        return "", ""
    parts = s[1:].split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _parse_workflow_inputs(recipe: dict, rest: str) -> dict:
    """Map a slash-command argument string to a workflow's named inputs.

    ``key=value`` tokens (quotes respected) set inputs explicitly; any leftover
    free text is assigned to the first not-yet-set input, preferring required
    ones — so ``/research-and-brief quantum computing`` fills ``topic``.
    """
    import shlex

    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    inputs: dict = {}
    leftover: list[str] = []
    for tok in tokens:
        if "=" in tok and tok.split("=", 1)[0].isidentifier():
            key, val = tok.split("=", 1)
            inputs[key] = val
        else:
            leftover.append(tok)
    if leftover:
        declared = recipe.get("inputs", []) or []
        target = next((i["name"] for i in declared if i["name"] not in inputs and i.get("required")), None)
        if target is None:
            target = next((i["name"] for i in declared if i["name"] not in inputs), None)
        if target:
            inputs[target] = " ".join(leftover)
    return inputs


def _parse_workflow_command(message: str):
    """Return (name, inputs) if ``message`` is ``/<known-workflow> …``, else None."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "workflow":
        return None
    recipe = STATE.workflow_registry.get(name)
    if recipe is None:  # defensive — _slash_kind already confirmed it
        return None
    return name, _parse_workflow_inputs(recipe, rest)


async def _run_parsed_workflow(name: str, inputs: dict, *, on_step=None) -> str:
    """Run a workflow command and format its output as the assistant reply.

    ``on_step`` is forwarded to the workflows plugin's runner (``STATE.workflow_run``,
    set when the plugin is enabled) so the caller can stream per-step progress (the
    chat path renders a tool card per step)."""
    if STATE.workflow_run is None:
        return "⚠️ workflows are not enabled"
    try:
        result = await STATE.workflow_run(name, inputs, on_step=on_step)
    except ValueError as exc:
        return f"⚠️ {exc}"
    if result.get("paused"):
        # Parked at a `gate: human` step — surface the SAME status block the
        # run_workflow tool returns (recipe, paused step, run id, prior step
        # outputs, resume paths), verbatim: it's already operator-facing text,
        # and the run isn't failed, so no failed-steps suffix applies.
        return result.get("output") or "⏸️ Workflow paused for operator approval."
    raw = result.get("output") or ""
    # Strip subagent scratch_pad/output tags so the chat shows clean text,
    # matching how a normal turn is rendered.
    out = extract_output(raw) or raw or "(workflow produced no output)"
    failed = result.get("failed") or []
    if failed:
        out += f"\n\n_(failed steps: {', '.join(failed)})_"
    return out


# --- Subagent slash commands (ADR 0020) --------------------------------------
# A chat message like ``/researcher find me X`` runs the named subagent instead
# of a normal model turn — the slash-command analogue of the ``task`` tool, so
# "run a worker" is a composer gesture, not a separate surface. Free text after
# the name is the subagent's prompt. A workflow of the same name wins (the turn
# dispatch checks workflows first). Short-circuits the turn like /goal does.


def _parse_subagent_command(message: str):
    """Return ``(subagent_type, prompt)`` if ``message`` is ``/<known-subagent>
    …`` (and not a workflow of the same name), else ``None``. Precedence is
    decided once by ``_slash_kind`` (a workflow of the same name wins)."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "subagent":
        return None
    return name, rest.strip()


async def _run_parsed_subagent(subagent_type: str, prompt: str) -> str:
    """Run one subagent from a chat slash command, formatted as the reply."""
    from graph.agent import run_manual_subagent

    try:
        raw = await run_manual_subagent(
            STATE.graph_config,
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            description=f"/{subagent_type} chat command",
            prompt=prompt,
            subagent_type=subagent_type,
        )
    except ValueError as exc:
        return f"⚠️ {exc}"
    # Strip the worker's scratch_pad/output tags so chat shows clean text.
    return extract_output(raw) or raw or "(subagent produced no output)"


# --- User-facing skill slash commands (ADR 0052) -----------------------------
# A chat message like ``/triage <args>`` runs a user-facing skill. Unlike a
# workflow/subagent command, it does NOT spawn a worker or short-circuit the
# turn — it REWRITES the message to inject the skill's procedure as a directive
# and falls through to the normal lead-agent turn, so every streaming / HITL /
# goal / tool invariant holds unchanged. Workflows and subagents of the same
# token win (dispatch checks them first).


# Slash-command precedence + the palette resolver live in graph/slash_commands.py
# — a neutral module both the dispatcher (here) and the console palette import, so
# the two can't drift (operator_api may not import server, so it can't live here).
from graph.slash_commands import (  # noqa: E402
    find_user_facing_skill as _find_user_facing_skill,
    slash_kind as _slash_kind,
)


def _parse_skill_command(message: str):
    """Return ``(skill_dict, args)`` if ``message`` is ``/<user-facing-skill> …``
    (and not ``/goal`` or a workflow/subagent of the same token), else ``None``.
    Precedence is decided once by ``_slash_kind``."""
    name, rest = _parse_slash_command(message)
    if not name or _slash_kind(name) != "skill":
        return None
    skill = _find_user_facing_skill(name)
    if skill is None:  # defensive — _slash_kind already confirmed it
        return None
    return skill, rest.strip()


def _skill_directive(skill: dict, args: str) -> str:
    """Compose the lead-agent directive injecting a user-facing skill's procedure
    into the turn (ADR 0052). The agent runs the procedure with its full toolset."""
    name = skill.get("name") or "skill"
    procedure = (skill.get("prompt_template") or "").strip()
    directive = f"[Running the '{name}' skill]\n\nFollow this procedure:\n\n{procedure}\n"
    if args:
        directive += f"\nInput: {args}\n"
    return directive

