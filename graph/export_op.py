"""On-demand chat export — the "share this thread" gesture (#2158 P1).

Serializes one thread's conversation to **self-contained Markdown** a human can read,
review, and send. The read-only sibling of ``compaction_op`` / ``rewind_op``: those
rewrite the live checkpoint, this one never mutates anything.

The pass, for one thread:

1. ``aget_state`` the current messages off the checkpoint.
2. **Redact** — scrub secrets out of message text and tool output.
3. **Render** — roles as headings, tool calls summarized, content preserved verbatim.

**Redaction is load-bearing here, not decoration.** An exported thread is meant to
*leave the machine*, and #2179 (P2) publishes this same bundle to a public URL. The
existing ``config_io.strip_secrets_from_doc`` / ``secret_paths`` machinery cannot help:
it is **config-shaped** — it strips known secret *keys* out of structured YAML. A chat
thread's secrets are **unstructured**: a token pasted into a message, an ``env`` dump in
tool output, an absolute path carrying the operator's username, a credentialed URL. So
this module does pattern-level redaction over free text, and — just as importantly —
*reports what it redacted*, so an operator can review before sharing rather than trust
a silent filter.

Redaction is deliberately **conservative about false negatives, tolerant of false
positives**: over-redacting a value costs a reader a little context, under-redacting one
leaks a live credential. Anything matched is replaced with ``[redacted:<kind>]``. It is a
safety net, **not a guarantee** — the returned ``redactions`` summary exists precisely so
a human still eyeballs the result.

Host-free and unit-testable: it takes the graph + checkpointer + thread id as arguments
(no ``STATE`` import), mirroring ``rewind_op.rewind_thread``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── redaction ────────────────────────────────────────────────────────────────────────
# (kind, pattern, replacement). Ordered most-specific first so a vendor token is named
# as such rather than swallowed by the generic KEY=value rule. Each replacement keeps
# enough shape for a reader to know *what* was removed without revealing the value.
_REDACTORS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # Vendor-shaped credentials — unambiguous, so match them before anything generic.
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[redacted:openai-key]"),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "[redacted:anthropic-key]"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted:github-token]"),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted:aws-access-key]"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted:slack-token]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted:jwt]"),
    # Private key blocks — take the whole armoured body, not just the header.
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        "[redacted:private-key]",
    ),
    # Authorization headers and credentialed URLs.
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [redacted:bearer-token]"),
    ("url-credentials", re.compile(r"://[^\s/:@]+:[^\s/@]+@"), "://[redacted:url-credentials]@"),
    # Generic `SOMETHING_SECRET=value` / `api_key: value` — the env-dump case. Keeps the
    # key name (useful context) and drops only the value.
    (
        "secret-assignment",
        re.compile(
            r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|password|passwd|credential)[A-Za-z0-9_.-]*)"
            r"(\s*[=:]\s*)(\"[^\"]+\"|'[^']+'|[^\s,;}\]]+)"
        ),
        r"\1\2[redacted:secret-assignment]",
    ),
    # Home directories leak the operator's username into anything shared.
    # NB: no leading \b — a word boundary can't match between a space and "/".
    ("home-path", re.compile(r"(/Users/|/home/|[A-Za-z]:\\Users\\)[^/\\\s\"']+"), r"\1[redacted:user]"),
)


def redact(text: str) -> tuple[str, list[str]]:
    """Scrub secrets from ``text``. Returns ``(redacted_text, kinds_found)``.

    ``kinds_found`` is de-duplicated and ordered by first appearance — it drives the
    "here's what was scrubbed" summary an operator reviews before sharing.
    """
    if not text:
        return text or "", []
    found: list[str] = []
    for kind, pattern, replacement in _REDACTORS:
        text, n = pattern.subn(replacement, text)
        if n and kind not in found:
            found.append(kind)
    return text, found


# ── rendering ────────────────────────────────────────────────────────────────────────
_ROLE_HEADINGS = {
    "human": "User",
    "user": "User",
    "ai": "Assistant",
    "assistant": "Assistant",
    "system": "System",
    "tool": "Tool result",
}


def _role_of(message) -> str:
    """The message's role, tolerant of LangChain objects and plain dicts."""
    for attr in ("type", "role"):
        value = getattr(message, attr, None) or (message.get(attr) if isinstance(message, dict) else None)
        if value:
            return str(value).lower()
    return type(message).__name__.replace("Message", "").lower() or "unknown"


def _text_of(message) -> str:
    """Message content as text. Multi-part content (the vision/tool-block shape) is
    flattened to its text parts so an export never renders a raw Python repr."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{type: text, text: …}, {type: image_url, …}]
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("type"):
                    parts.append(f"_[{block['type']}]_")
        return "\n\n".join(parts)
    return "" if content is None else str(content)


def _tool_calls_of(message) -> list[dict]:
    calls = getattr(message, "tool_calls", None)
    if not calls and isinstance(message, dict):
        calls = message.get("tool_calls")
    return list(calls or [])


def render_markdown(
    messages: list,
    *,
    thread_id: str,
    title: str | None = None,
    exported_at: str | None = None,
    redact_secrets: bool = True,
) -> tuple[str, list[str]]:
    """Render ``messages`` to self-contained Markdown. Returns ``(markdown, redactions)``.

    System messages are **excluded** — an export is a conversation to share, and the
    system prompt is the agent's configuration (frequently sensitive, never part of what
    the two parties said). Tool calls are summarized by name + arguments rather than
    dumped raw, so the transcript stays readable.
    """
    stamp = exported_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    redactions: list[str] = []

    def _scrub(text: str) -> str:
        if not redact_secrets:
            return text
        cleaned, kinds = redact(text)
        for k in kinds:
            if k not in redactions:
                redactions.append(k)
        return cleaned

    lines: list[str] = [f"# {title or 'Chat export'}", "", f"_Exported {stamp} · thread `{thread_id}`_", ""]

    shown = 0
    for message in messages:
        role = _role_of(message)
        if role == "system":  # agent configuration, not conversation
            continue
        heading = _ROLE_HEADINGS.get(role, role.title() or "Message")
        body = _scrub(_text_of(message)).strip()
        calls = _tool_calls_of(message)

        if not body and not calls:
            continue
        shown += 1
        lines.append(f"## {heading}")
        lines.append("")
        if body:
            lines.append(body)
            lines.append("")
        for call in calls:
            name = call.get("name") or "tool"
            args = call.get("args")
            lines.append(f"> **Tool call** `{name}`")
            if args:
                lines.append(f">\n> ```json\n> {_scrub(str(args))}\n> ```")
            lines.append("")

    if not shown:
        lines.append("_This thread has no shareable messages._")
        lines.append("")

    if redactions:
        lines.append("---")
        lines.append("")
        lines.append(
            "> **Redacted before export:** "
            + ", ".join(f"`{k}`" for k in redactions)
            + ". Values matching known secret shapes were replaced with `[redacted:…]`. "
            "This is a safety net, not a guarantee — read the transcript before sharing it."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", redactions


# ── the op ───────────────────────────────────────────────────────────────────────────
async def export_thread(
    graph,
    checkpointer,
    thread_id: str,
    *,
    title: str | None = None,
    redact_secrets: bool = True,
) -> dict:
    """Export ``thread_id``'s conversation as Markdown.

    Returns ``{found, markdown, message_count, redactions, reason}``. ``found`` is false
    (with empty markdown) when there's no checkpointer or the thread has no state — an
    empty thread is *not* an error, just nothing to share. **Never mutates the
    checkpoint**: unlike its compaction/rewind siblings this is a pure read.
    """
    if graph is None or checkpointer is None:
        return {"found": False, "markdown": "", "message_count": 0, "redactions": [], "reason": "no_checkpointer"}

    lg_config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(lg_config)
    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])

    if not messages:
        return {"found": False, "markdown": "", "message_count": 0, "redactions": [], "reason": "empty_thread"}

    markdown, redactions = render_markdown(messages, thread_id=thread_id, title=title, redact_secrets=redact_secrets)
    if redactions:
        log.info("[export] thread %s: redacted %s", thread_id, ", ".join(redactions))
    return {
        "found": True,
        "markdown": markdown,
        "message_count": len(messages),
        "redactions": redactions,
        "reason": "ok",
    }
