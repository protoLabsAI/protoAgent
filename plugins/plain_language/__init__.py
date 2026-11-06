"""Plain Language — clearer writing for every reply, from digital.gov's plain language guide.

Three layers, because a skill only loads when the agent judges it relevant and so
can't shape an ordinary chat reply on its own:

1. **Always-on core rules** — ``PlainLanguageMiddleware`` appends the short rule
   block in ``core.py`` to the system message on every model call. The block never
   changes, so it caches with the rest of the stable prefix. Turn it off with the
   ``always_on`` setting.
2. **Skills the agent loads itself** — ``plain-language-write`` (the full checklist
   for substantial drafts) and ``plain-language-edit`` (a rewrite pass on existing
   text).
3. **Slash commands** — ``/plain-review`` (findings by severity) and
   ``/paraphrase-test`` (simulated readers paraphrase the text, via the
   ``plain_reader`` delegate).

Plus one tool, ``plain_language_check``: the deterministic linter in ``lint.py``,
so mechanical rules (filler, complex words, "and/or", "click here", paragraph
length, heading depth) are checked the same way every time. No network and no
filesystem access.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

from . import lint
from .core import CORE_RULES, MARK

log = logging.getLogger("protoagent.plugins.plain_language")

_REGISTRY = None


def _cfg(key: str, default):
    """A live config read (ADR 0019), so a Settings change applies on the next turn."""
    if _REGISTRY is None:
        return default
    try:
        value = (_REGISTRY.live_config() or {}).get(key)
        return default if value is None else value
    except Exception:  # noqa: BLE001 — config trouble must never break a turn
        return default


def with_core_rules(sysmsg):
    """``sysmsg`` with the core rule block as its final text block.

    Idempotent: a block already carrying the marker is replaced, never stacked.
    Returns ``None`` when there is nothing safe to attach to.
    """
    if sysmsg is None:
        return None
    block = {"type": "text", "text": CORE_RULES}
    content = getattr(sysmsg, "content", None)
    if isinstance(content, str):
        blocks = ([{"type": "text", "text": content}] if content else []) + [block]
    elif isinstance(content, list):
        blocks = [b for b in content if not (isinstance(b, dict) and str(b.get("text", "")).startswith(MARK))]
        blocks.append(block)
    else:
        return None
    return sysmsg.model_copy(update={"content": blocks})


class PlainLanguageMiddleware(AgentMiddleware):
    """Append the always-on plain-language rules to the system message."""

    def _transform(self, request):
        if not bool(_cfg("always_on", True)):
            return request
        try:
            new = with_core_rules(getattr(request, "system_message", None))
        except Exception as exc:  # noqa: BLE001 — never break a turn over style guidance
            log.debug("[plain_language] could not attach core rules: %s", exc)
            return request
        return request if new is None else request.override(system_message=new)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))


@tool
def plain_language_check(text: str) -> str:
    """Check a draft against the plain-language rules and list what to fix.

    Use it on a long draft (roughly 300 words or more) that someone will read: a doc,
    email, report, release note, or long answer. Run it before you hand the draft over,
    or when the operator asks whether something is clear. Skip it for short replies and
    short messages; reread those yourself instead. Don't mention the check in your
    reply unless the operator asked for it: give them the finished text. It flags filler, complex words,
    doublets, hidden verbs, possible passive voice, "and/or", "e.g.", "shall", vague
    link text, long sentences and paragraphs, deep headings, lists without a lead-in,
    and undefined abbreviations. Code blocks, inline code and URLs are skipped.

    Findings are heuristics: fix the clear ones, and judge passive-voice and
    hidden-verb hits by reading the sentence. A clean result doesn't prove the text
    is clear to its reader.
    """
    return lint.format_report(lint.check(text))


def _plain_reader():
    """A delegate that reads text as one kind of reader would and paraphrases it."""
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="plain_reader",
        description=(
            "Reads a piece of writing as a specific reader would (give it the persona and "
            "the text) and reports back: a paraphrase in its own words, what it would do "
            "next, and what confused it. Used by /paraphrase-test."
        ),
        system_prompt=(
            "You are a test reader for a plain-language paraphrase test.\n\n"
            "The task gives you a reader persona and a text. Become that reader: use only "
            "the knowledge that persona has. Don't use what you know as an AI model to "
            "fill gaps the text leaves. If the persona wouldn't know a term, treat it as "
            "unknown.\n\n"
            "Read the text once, as that reader would. Then answer in this order, "
            "under these exact headings:\n"
            "PARAPHRASE: for each section (or each paragraph of a short text), say in "
            "one or two plain sentences what it tells you. Use your own words, not the text's.\n"
            "NEXT ACTION: what you would do after reading this, as a concrete step.\n"
            "CONFUSING: each word, sentence or step you didn't understand or could read "
            "two ways, quoted exactly, with what you thought it meant.\n"
            "MISSING: anything you needed to act and couldn't find.\n\n"
            "Be honest. If you misunderstood something, the writer needs to see it. "
            "Don't rewrite the text and don't grade it."
        ),
        tools=[],
        inherit_read_tools=False,
        allow_skill_emission=False,
        max_turns=4,
    )


def register(registry) -> None:
    """Entry point: skills, the core-rules middleware, the check tool, the reader delegate."""
    global _REGISTRY
    _REGISTRY = registry
    try:
        registry.register_skill_dir("skills")
    except Exception:
        log.exception("[plain_language] failed to register skill dir")
    try:
        registry.register_middleware(lambda config: PlainLanguageMiddleware())
    except Exception:
        log.exception("[plain_language] failed to register middleware")
    try:
        registry.register_tool(plain_language_check)
    except Exception:
        log.exception("[plain_language] failed to register plain_language_check")
    try:
        registry.register_subagent(_plain_reader())
    except Exception:
        log.exception("[plain_language] failed to register plain_reader")
