"""Persona untooled-action audit — the runtime detection tier of issue #2276.

A persona (SOUL.md) can command an action that no registered tool backs, and the
failure is silent BY DESIGN: the model never *calls* a missing tool, so there is no
error, no crash, no tool-not-found message — it fills the impossible instruction with
narration and reports the action as completed. The concrete case: the project-manager
preset said pain points "get filed as issues" while ``github_create_issue`` was gated
off (``github.write`` defaults false); the agent reported filings that had never
happened, and the operator discovered the breakage out-of-band.

#2273 fixed that preset's *prose* (an untooled action is reported as a request, never
claimed as done). This module is the HOST-side seam: a deterministic, read-only diff of
what the persona commits to against what the built graph actually binds. Two signals:

* **tool mentions** — snake_case tokens shaped like tool identifiers (verb-led, e.g.
  ``run_command``, ``edit_soul``) that match no bound tool. Infrastructure identifiers
  (``a2a_impl``, ``host_config``) carry no action verb and never warn.
* **capabilities** — a small curated lexicon of natural-language commitments ("file
  issues", "send email", "post to Slack") mapped to tool-name patterns; a phrase whose
  pattern matches zero bound tool names is a promise the agent cannot keep.

Warn-only (the passive tier #2276 asks to start with): the caller logs findings and
publishes ``persona.untooled_action_detected`` — nothing blocks a persona from loading,
because "I want this tool but haven't configured it yet" is a legitimate state. Pure
functions of (persona text, tool names): no clock, no I/O, no model — identical inputs
always yield identical findings, so the guarded/refuse tier can be layered on later
without re-litigating detection.
"""

from __future__ import annotations

import re

# Tokens shaped like tool identifiers: lowercase snake_case, two or more segments.
_TOOLISH_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# A snake_case token only counts as tool-shaped when one of its segments is an action
# verb — the tool namespace is verb-led (create_*, *_send_*, run_*), while the
# infrastructure namespace personas also mention (``a2a_impl``, ``soul_history``,
# ``host_config``) is noun-led. This is the false-positive fence: prose identifiers
# without a verb segment are never findings.
_ACTION_VERBS = frozenset(
    {
        "add",
        "cancel",
        "close",
        "create",
        "delete",
        "edit",
        "execute",
        "fetch",
        "file",
        "ingest",
        "open",
        "post",
        "publish",
        "recall",
        "remove",
        "run",
        "schedule",
        "search",
        "send",
        "submit",
        "update",
        "upload",
        "write",
    }
)

# Curated capability lexicon: (label, phrase the persona commits with, pattern a bound
# tool name must match for the commitment to be backed). Deliberately small and
# high-precision — every entry is a failure class that has actually bitten or is one
# rename away from the one that did. Phrases are prose (case-insensitive) and bounded
# to one sentence (no ``.`` or newline between verb and object); tool patterns run over
# the lowercase bound-tool names. Grow this list one confirmed miss at a time; a fuzzy
# entry turns the audit into noise and gets it ignored.
_CAPABILITIES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "file issues",
        # "file/filed/create/raise … issue(s)" — explicit endings (not ``fil[a-z]*``,
        # which would match "filter … issues") and NOT "open issues", which is how
        # personas describe *reading* the queue ("review the open issues").
        re.compile(r"\b(?:fil(?:e|es|ed|ing)|creat(?:e|es|ed|ing)|rais(?:e|es|ed|ing))\b[^.\n]{0,40}\bissues?\b", re.I),
        re.compile(r"(?:create|file|open|submit|new)[a-z0-9_]*issue|issue[a-z0-9_]*(?:create|file|submit)"),
    ),
    (
        "send email",
        re.compile(r"\bsend(?:s|ing)?\b[^.\n]{0,30}\be-?mails?\b", re.I),
        re.compile(r"(?:send|compose|draft|create)[a-z0-9_]*mail|mail[a-z0-9_]*(?:send|compose|draft)"),
    ),
    (
        "post to Slack",
        re.compile(r"\b(?:post|send|messag|repl|announc)[a-z]*\b[^.\n]{0,30}\bslack\b|\bslack\b[^.\n]{0,30}\b(?:post|send|messag|announc)[a-z]*\b", re.I),
        re.compile(r"slack"),
    ),
    (
        "post to Discord",
        re.compile(r"\b(?:post|send|messag|repl|announc)[a-z]*\b[^.\n]{0,30}\bdiscord\b|\bdiscord\b[^.\n]{0,30}\b(?:post|send|messag|announc)[a-z]*\b", re.I),
        re.compile(r"discord"),
    ),
    (
        "manage calendar events",
        re.compile(r"\b(?:creat|schedul|book|add)[a-z]*\b[^.\n]{0,30}\b(?:calendar|meeting|event)s?\b", re.I),
        re.compile(r"calendar|meeting|event"),
    ),
    (
        "open pull requests",
        re.compile(r"\b(?:open|creat|submit|rais|merg)[a-z]*\b[^.\n]{0,30}\bpull.?requests?\b", re.I),
        re.compile(r"pull_request|merge_pr|create_pr|open_pr"),
    ),
    (
        "run shell commands",
        re.compile(r"\b(?:run|execut)[a-z]*\b[^.\n]{0,25}\b(?:shell|bash|terminal|command|script)s?\b", re.I),
        re.compile(r"run_command|execute|shell|bash|terminal"),
    ),
)


def _evidence(text: str, start: int, end: int, margin: int = 40) -> str:
    """A one-line snippet around ``text[start:end]`` — the finding's receipt, so a
    warning can show *where* the persona makes the commitment without the reader
    re-grepping SOUL.md. Whitespace-collapsed; ellipsized when clipped."""
    lo, hi = max(0, start - margin), min(len(text), end + margin)
    snippet = " ".join(text[lo:hi].split())
    return f"{'…' if lo > 0 else ''}{snippet}{'…' if hi < len(text) else ''}"


def audit_untooled_actions(soul_text: str, tool_names) -> list[dict]:
    """Diff the persona's committed actions against the bound tool set.

    Returns a list of findings, each ``{kind, action, evidence}``:

    * ``kind="tool_mention"`` — ``action`` is a verb-led snake_case token from the
      persona that matches no bound tool name exactly.
    * ``kind="capability"`` — ``action`` is a lexicon label whose phrase appears in the
      persona while no bound tool name matches its pattern.

    Deduplicated (one finding per token / per capability, first occurrence's evidence),
    deterministic, and pure — the caller decides what a finding *does* (log, publish,
    refuse); this function only detects. An empty persona or an empty findings state
    returns ``[]``.
    """
    text = soul_text or ""
    if not text.strip():
        return []
    names = {str(n) for n in (tool_names or ())}
    findings: list[dict] = []

    seen_tokens: set[str] = set()
    for m in _TOOLISH_TOKEN_RE.finditer(text):
        token = m.group(0)
        if token in names or token in seen_tokens:
            continue
        if not any(seg in _ACTION_VERBS for seg in token.split("_")):
            continue
        seen_tokens.add(token)
        findings.append({"kind": "tool_mention", "action": token, "evidence": _evidence(text, m.start(), m.end())})

    # Patterns are segment-bounded (no whitespace atoms), so a newline join can't
    # produce a cross-name match.
    joined_names = "\n".join(sorted(n.lower() for n in names))
    for label, phrase_re, tool_re in _CAPABILITIES:
        m = phrase_re.search(text)
        if m is None or tool_re.search(joined_names):
            continue
        findings.append({"kind": "capability", "action": label, "evidence": _evidence(text, m.start(), m.end())})

    return findings
