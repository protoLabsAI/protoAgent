"""Structured chat-bundle export (#2680/#2681) — the P2 (#2179) building block P1's
markdown export doesn't produce: a versioned, machine-readable manifest a hosted viewer can
render close to 1:1 with the console's own chat UI (ordered text/tool-call parts, inline
artifacts), rather than a flattened ``.md`` dump.

Sibling to ``graph.export_op``, not a replacement — ``/export`` still produces markdown via
``export_thread`` today, untouched by this module. Reuses ``export_op``'s message-shape
helpers (``role_of`` / ``text_of`` / ``tool_calls_of``) and its ``redact()`` pattern-sweep
rather than re-implementing them: same fail-closed redaction, same "system messages are
configuration, not conversation, never exported" rule.

**Artifacts are resolved through an injected callback, not a direct import of
``plugins.artifact``.** Nothing in ``graph/``, ``server/``, or ``operator_api/`` imports a
specific plugin today — plugins are meant to be optional and swappable, and the artifact
plugin, though shipped in-tree and on by default, is still a plugin an operator can disable.
Keeping this op host-free (explicit inputs, no ``STATE``, mirrors ``export_op`` /
``snapshot_op``) and accepting a resolver as a parameter means: unit tests need no plugin
loaded, and a caller with the plugin disabled gets ``available: False`` artifact parts
instead of an ``ImportError``. The real resolver a live caller wires in is
``plugins.artifact.resolve_for_bundle``, imported defensively at the call site (typically
``server/chat.py``, which already owns thread resolution for export).

**Which artifact VERSION gets bundled is the one genuinely tricky design point here** — see
``plugins/artifact/__init__.py``'s ``resolve_for_bundle`` docstring for why a reported
version number is trustworthy as an index only until that artifact's version count has ever
exceeded its retention cap, and how that's detected structurally rather than guessed.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from graph.export_op import redact
from graph.message_blocks import role_of, text_of, tool_calls_of

BUNDLE_VERSION = 1

# (artifact_id, version|None) — version is None only for a fresh `show_artifact` creation,
# which is always that artifact's sole (index-0) version at the time.
ArtifactResolver = Callable[[str, "int | None"], dict]

_ARTIFACT_ID = r"a-\d{10,}-[0-9a-f]{6}"
_ARTIFACT_TOOLS = {"show_artifact", "update_artifact", "rewrite_artifact", "save_file_artifact"}

# How to recover (artifact_id, version) from an artifact tool's RESULT text — the only place
# a thread names which version a specific call produced (call args don't carry it; see
# plugins/artifact/__init__.py's return strings for each tool). `has_version=False` means
# "no version group to capture" (show_artifact — always the creation, version None/index 0).
_ARTIFACT_RESULT_PATTERNS: tuple[tuple[str, re.Pattern, bool], ...] = (
    ("show_artifact", re.compile(rf"\bartifact ({_ARTIFACT_ID}) \("), False),
    ("update_artifact", re.compile(rf"\bartifact ({_ARTIFACT_ID}) → version (\d+)"), True),
    ("rewrite_artifact", re.compile(rf"\bartifact ({_ARTIFACT_ID}) → version (\d+)"), True),
    ("save_file_artifact", re.compile(rf"\bartifact ({_ARTIFACT_ID}) → v(\d+)"), True),
)


def _parse_artifact_reference(tool_name: str, result_text: str) -> tuple[str, int | None] | None:
    """Recover ``(artifact_id, version)`` from an artifact tool's result text, if present.

    Prose, not a structured return. A future wording change in ``plugins/artifact``
    degrades this to "no reference found" (the tool-call part just renders with no artifact
    attached) rather than raising — this is a best-effort enrichment layered on top of a
    still-complete export, never something the export depends on to succeed.
    """
    for name, pattern, has_version in _ARTIFACT_RESULT_PATTERNS:
        if name != tool_name:
            continue
        m = pattern.search(result_text or "")
        if not m:
            return None
        return m.group(1), (int(m.group(2)) if has_version else None)
    return None


def _default_resolver(_artifact_id: str, _version: "int | None") -> dict:
    return {"available": False, "reason": "artifact plugin not available"}


def _artifact_field(tool_name: str, args: dict, result_text: str, resolver: ArtifactResolver) -> dict | None:
    """The ``artifact`` field attached to a tool_call part, or ``None`` when this call
    didn't touch an artifact (wrong tool, or no reference could be parsed out of it)."""
    if tool_name not in _ARTIFACT_TOOLS:
        return None
    ref = _parse_artifact_reference(tool_name, result_text)
    if ref is None:
        return None
    art_id, version = ref

    # show_artifact / rewrite_artifact carry the FULL text right in the call's own args —
    # use it directly instead of round-tripping through the version store, which is exactly
    # what this turn submitted and carries no trim-availability risk at all.
    if tool_name in ("show_artifact", "rewrite_artifact") and isinstance(args.get("code"), str):
        # `version` is None for show_artifact (the parser has no group to capture — a fresh
        # creation has no NUMBER in its result text at all) but is unambiguously version 1;
        # None stays a valid internal "use the sole version" sentinel elsewhere (see
        # resolve_for_bundle), just not a good look in the actual JSON this produces.
        return {
            "id": art_id,
            "artifact_kind": args.get("kind") or "",
            "title": args.get("title") or "",
            "version": 1 if tool_name == "show_artifact" else version,
            "available": True,
            "content": args["code"],
        }

    resolved = resolver(art_id, version) or {}
    part: dict = {
        "id": art_id,
        "artifact_kind": resolved.get("kind", ""),
        "title": resolved.get("title", ""),
        "available": bool(resolved.get("available")),
    }
    if part["available"]:
        part["version"] = resolved.get("version")
        part["content"] = resolved.get("code", "")
    else:
        part["reason"] = resolved.get("reason", "unavailable")
        if resolved.get("file_meta"):
            part["file_meta"] = resolved["file_meta"]
    return part


def _tool_result_texts(messages: list) -> dict[str, str]:
    """``tool_call_id -> result text`` for every ``ToolMessage`` in the thread, so a
    tool_call part (built from the calling AIMessage) can carry its own result inline —
    matching the console's model (``ChatMessage.toolCalls[].output``), where a tool result
    is never its own chat bubble."""
    out: dict[str, str] = {}
    for message in messages:
        if role_of(message) != "tool":
            continue
        call_id = getattr(message, "tool_call_id", None) or (
            message.get("tool_call_id") if isinstance(message, dict) else None
        )
        if call_id:
            out[str(call_id)] = text_of(message)
    return out


def build_bundle(
    messages: list,
    *,
    thread_id: str,
    title: str | None = None,
    exported_at: str | None = None,
    redact_secrets: bool = True,
    artifact_resolver: ArtifactResolver | None = None,
) -> tuple[dict, list[str]]:
    """Build the structured manifest from ``messages``. Returns ``(manifest, redactions)``.

    Pure — no I/O, no artifact-store access of its own (that's ``artifact_resolver``'s job).
    System messages are excluded, same rule as ``export_op.render_markdown``. Tool results
    are folded into their calling tool_call part rather than kept as separate ``tool``-role
    entries, mirroring the console's own message model instead of the raw LangGraph list.
    """
    resolver = artifact_resolver or _default_resolver
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

    results_by_call_id = _tool_result_texts(messages)
    out_messages: list[dict] = []

    for message in messages:
        role = role_of(message)
        if role in ("system", "tool"):  # config, and folded into the caller respectively
            continue
        # Injected context frames (#2776) are runtime machinery, not conversation —
        # same rule as the Markdown export: never ship recalled memory on a bundle
        # headed for a public link.
        from graph.context_frame import is_context_frame

        if is_context_frame(message):
            continue
        parts: list[dict] = []
        body = _scrub(text_of(message)).strip()
        if body:
            parts.append({"kind": "text", "text": body})

        for call in tool_calls_of(message):
            call_id = str(call.get("id") or "")
            name = call.get("name") or "tool"
            args = call.get("args") or {}
            result_text = results_by_call_id.get(call_id, "")
            part: dict = {
                "kind": "tool_call",
                "id": call_id,
                "name": name,
                "input": args,
                "output": _scrub(result_text) if result_text else "",
            }
            artifact = _artifact_field(name, args, result_text, resolver)
            if artifact is not None:
                if artifact.get("available") and isinstance(artifact.get("content"), str):
                    artifact["content"] = _scrub(artifact["content"])
                part["artifact"] = artifact
            parts.append(part)

        if not parts:
            continue
        out_messages.append({"role": "user" if role in ("human", "user") else "assistant", "parts": parts})

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "kind": "chat-bundle",
        "exported_at": stamp,
        "thread_id": thread_id,
        "title": title or "Chat export",
        "messages": out_messages,
    }
    return manifest, redactions


async def export_bundle(
    graph,
    checkpointer,
    thread_id: str,
    *,
    title: str | None = None,
    redact_secrets: bool = True,
    artifact_resolver: ArtifactResolver | None = None,
) -> dict:
    """Structured sibling of ``export_op.export_thread`` — same read-only contract (never
    touches the checkpoint), same ``{found, ..., reason}`` shape, ``manifest`` instead of
    ``markdown``.
    """
    if graph is None or checkpointer is None:
        return {"found": False, "manifest": None, "message_count": 0, "redactions": [], "reason": "no_checkpointer"}

    lg_config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(lg_config)
    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])

    if not messages:
        return {"found": False, "manifest": None, "message_count": 0, "redactions": [], "reason": "empty_thread"}

    manifest, redactions = build_bundle(
        messages,
        thread_id=thread_id,
        title=title,
        redact_secrets=redact_secrets,
        artifact_resolver=artifact_resolver,
    )
    return {
        "found": True,
        "manifest": manifest,
        "message_count": len(messages),
        "redactions": redactions,
        "reason": "ok",
    }


# ── packaging ──────────────────────────────────────────────────────────────────────────
BUNDLE_MANIFEST = "manifest.json"


@dataclass
class ChatBundleResult:
    """The built bundle plus everything an operator needs to review before publishing it —
    same shape and purpose as ``snapshot_op.SnapshotResult``."""

    data: bytes
    filename: str
    manifest: dict
    redactions: list[str] = field(default_factory=list)
    #: Human-facing notes on artifacts that could NOT be fully included (binary, or
    #: trimmed by the plugin's own retention policy) — surfaced in REVIEW.md so the
    #: operator knows what a viewer of this bundle will NOT see, not just what was scrubbed.
    artifact_notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "bytes": len(self.data),
            "redactions": self.redactions,
            "artifact_notes": self.artifact_notes,
        }


def _unavailable_artifact_notes(manifest: dict) -> list[str]:
    """Walk the built manifest for tool_call parts whose ``artifact`` is unavailable — the
    "here's what a viewer of this bundle will NOT see" list for REVIEW.md."""
    notes: list[str] = []
    for message in manifest.get("messages", []):
        for part in message.get("parts", []):
            artifact = part.get("artifact") if part.get("kind") == "tool_call" else None
            if artifact and not artifact.get("available"):
                kind = artifact.get("artifact_kind") or "artifact"
                notes.append(f"`{artifact.get('id', '?')}` ({kind}) — {artifact.get('reason', 'unavailable')}")
    return notes


def render_bundle_review(*, title: str, exported_at: str, redactions: list[str], artifact_notes: list[str]) -> str:
    """The operator-facing review, written INTO the zip as ``REVIEW.md`` — same reasoning as
    ``snapshot_op.render_review`` and the #2158 chat-export note: it lives inside the
    artifact, not only in an API response, so it can never be separated from what it
    describes. An export is meant to leave the machine; the operator reviews rather than
    trusting a silent filter.
    """
    lines = [f"# Chat bundle review — {title}", "", f"Exported {exported_at}.", ""]

    lines += ["## Redacted before export", ""]
    if redactions:
        lines += [
            "Values matching known secret shapes were replaced with `[redacted:kind]` "
            "throughout this bundle (message text, tool output, and artifact content alike).",
            "",
            *[f"- `{k}`" for k in redactions],
        ]
    else:
        lines.append("*None — nothing matched a known secret shape.*")

    lines += ["", "## Artifacts not fully included", ""]
    if artifact_notes:
        lines += [
            "A viewer of this bundle will NOT see the following — either a binary "
            "attachment (never bundled) or a version this thread referenced that the "
            "artifact plugin's own retention policy has since trimmed away:",
            "",
            *[f"- {n}" for n in artifact_notes],
        ]
    else:
        lines.append("*None — every artifact this thread referenced is included.*")

    lines += [
        "",
        "## Caveat",
        "",
        "Pattern redaction is a **safety net, not a guarantee** — it is deliberately "
        "conservative about false negatives, but it cannot recognize a credential that "
        "looks like ordinary prose. Read this bundle before publishing it.",
        "",
    ]
    return "\n".join(lines)


def build_bundle_zip(manifest: dict, redactions: list[str]) -> ChatBundleResult:
    """Package a built manifest (from ``build_bundle`` / ``export_bundle``) into the bundle
    zip: ``manifest.json`` + ``REVIEW.md``. In-memory, mirroring ``snapshot_op.build_snapshot``.

    No separate per-artifact files today — inline text/code artifact content already lives
    in ``manifest.json`` (the "no second serializer" design call on #2680), and binary
    artifacts are placeholder-only (#2179's P2 scoping decision). The zip container is
    still the right shape now rather than a bare ``.json`` file: it's what #2685's viewer
    contract expects, and it's where a future binary-attachment slice would add
    ``artifacts/<id>-v<n>.<ext>`` members without changing the format again.
    """
    artifact_notes = _unavailable_artifact_notes(manifest)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(BUNDLE_MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
        # Written LAST: it describes the finished artifact, not a partial one.
        zf.writestr(
            "REVIEW.md",
            render_bundle_review(
                title=manifest.get("title") or "Chat export",
                exported_at=manifest.get("exported_at") or "",
                redactions=redactions,
                artifact_notes=artifact_notes,
            ),
        )
    thread_id = manifest.get("thread_id") or "thread"
    return ChatBundleResult(
        data=buf.getvalue(),
        filename=f"chat-bundle-{thread_id}.zip",
        manifest=manifest,
        redactions=redactions,
        artifact_notes=artifact_notes,
    )
