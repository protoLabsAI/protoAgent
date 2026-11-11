"""Bounded, in-memory native execution receipts; no review policy or persistence."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from uuid import uuid4

from langchain_core.messages import ToolMessage

from graph.middleware.redaction import redact

_LIMITS = {
    "entries": 128,
    "depth": 6,
    "fields": 32,
    "nodes": 256,
    "field_bytes": 512,
    "entry_bytes": 8192,
    "aggregate_bytes": 131072,
}
_CURRENT: ContextVar[ToolReceiptCapture | None] = ContextVar("native_tool_receipts", default=None)
# Payload/credential fields are omitted even when their values have no recognizable
# token shape. Normalize field spelling just as the shared redactor does.
_OMIT_KEYS = {
    "content",
    "contents",
    "body",
    "text",
    "data",
    "output",
    "stdout",
    "stderr",
    "filecontent",
    "filecontents",
    "imageurl",
    "base64",
    "payload",
    "password",
    "passwd",
    "token",
    "secret",
    "credentials",
}

# Result artifacts are arbitrary tool-owned data, not automatically metadata.
# Keep useful evidence fields; unknown leaves may be file text under novel keys.
_RESULT_FIELDS = {
    "status",
    "iserror",
    "success",
    "ok",
    "path",
    "paths",
    "filepath",
    "filepaths",
    "filename",
    "filenames",
    "mimetype",
    "size",
    "sizebytes",
    "bytes",
    "count",
    "total",
    "linestart",
    "lineend",
    "startline",
    "endline",
    "linecount",
    "linesread",
    "truncated",
    "exitcode",
    "returncode",
    "sha",
    "sha256",
    "commit",
    "revision",
}

_RESULT_TEXT_FIELDS = {
    "status",
    "path",
    "paths",
    "filepath",
    "filepaths",
    "filename",
    "filenames",
    "mimetype",
    "sha",
    "sha256",
    "commit",
    "revision",
}


class _SafeValue:
    def __init__(self):
        self.dropped = 0
        self.nodes = 0

    def omit(self):
        self.dropped += 1
        return "[OMITTED]"

    def value(self, value, depth=0, *, metadata=False, field=""):
        if metadata and type(value) not in (dict, list, tuple) and field not in _RESULT_FIELDS:
            return self.omit()
        self.nodes += 1
        if depth > _LIMITS["depth"] or self.nodes > _LIMITS["nodes"]:
            return self.omit()
        if value is None or type(value) is bool:
            return value
        if type(value) in (int, float):
            if (type(value) is int and value.bit_length() > 64) or (type(value) is float and not math.isfinite(value)):
                return self.omit()
            return value
        if type(value) is str:
            if metadata and field not in _RESULT_TEXT_FIELDS:
                return self.omit()
            safe = redact(value).encode("utf-8", errors="replace")
            if len(safe) > _LIMITS["field_bytes"]:
                self.dropped += 1
                safe = safe[: _LIMITS["field_bytes"]]
            return safe.decode("utf-8", errors="ignore")
        if type(value) is dict:
            out = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= _LIMITS["fields"]:
                    self.dropped += len(value) - i
                    break
                if type(key) is not str:
                    self.dropped += 1
                    continue
                # Use the shared key-sensitive redactor before bounding either key
                # or value. Never traverse an unbounded subtree in redact().
                sensitive = redact({key: None})[key] is not None
                safe_key = self.value(key, depth + 1)
                if safe_key in out:
                    self.dropped += 1
                out[safe_key] = (
                    "[REDACTED]"
                    if sensitive
                    else self.omit()
                    if key.casefold().replace("_", "").replace("-", "") in _OMIT_KEYS
                    else self.value(
                        item, depth + 1, metadata=metadata, field=key.casefold().replace("_", "").replace("-", "")
                    )
                )
            return out
        if type(value) in (list, tuple):
            self.dropped += max(0, len(value) - _LIMITS["fields"])
            return [self.value(item, depth + 1, metadata=metadata, field=field) for item in value[: _LIMITS["fields"]]]
        return self.omit()


class ToolReceiptCapture:
    """One invocation's collector. Read detached JSON-compatible data with snapshot()."""

    def __init__(self):
        self.invocation_id = uuid4().hex
        self._lock = Lock()
        self._supported = False
        self._closed = False
        self._partial = False
        self._sequence = 0
        self._pending = 0
        self._dropped = 0
        self._bytes = 0
        self._entries = []
        self.review_context_callback = None
        self._context_observed = False

    def snapshot(self) -> dict:
        """Return a detached version-1 envelope, including limits and loss counts."""
        with self._lock:
            status = (
                "unsupported"
                if not self._supported
                else "partial"
                if (self._partial or self._dropped or self._pending)
                else "captured"
            )
            return {
                "schema_version": 1,
                "source": "native_audit",
                "invocation_id": self.invocation_id,
                "capture_status": status,
                "closed": self._closed,
                "limits": dict(_LIMITS),
                "dropped_count": self._dropped,
                "pending_count": self._pending,
                "partial_count": sum(e["partial"] for e in self._entries),
                "entries": copy.deepcopy(sorted(self._entries, key=lambda e: e["sequence"])),
            }

    def _support(self):
        with self._lock:
            if not self._closed:
                self._supported = True

    def _begin(self, tool_call):
        with self._lock:
            if self._closed:
                return None
            self._supported = True
            self._sequence += 1
            if self._sequence > _LIMITS["entries"]:
                self._dropped += 1
                return None
            self._pending += 1
            sequence = self._sequence
        safe = _SafeValue()
        entry = {"invocation_id": self.invocation_id, "execution_id": uuid4().hex, "sequence": sequence}
        try:
            entry.update(
                tool_call_id=safe.value(tool_call.get("id")),
                name=safe.value(tool_call.get("name")),
                args=safe.value(tool_call.get("args", {})),
            )
        except Exception:
            # Receipt formatting must never prevent the physical handler running.
            safe.dropped += 1
            entry.update(tool_call_id=None, name="[OMITTED]", args="[OMITTED]")
        return entry, safe, time.monotonic()

    def _finish(self, ticket, result, error):
        entry, safe, started = ticket
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        state = "success"
        metadata = {}
        if error is not None:
            state = "cancelled" if isinstance(error, (asyncio.CancelledError, FutureCancelledError)) else "exception"
            metadata = {"error_type": safe.value(type(error).__name__)}
        elif isinstance(result, ToolMessage):
            state = "tool_error" if result.status == "error" else "success"
            # Content is deliberately never copied, including short file bodies.
            metadata = {
                "kind": "tool_message",
                "status": safe.value(result.status),
                "content_kind": "text" if type(result.content) is str else "multipart",
                "content_items": len(result.content),
                "artifact": safe.value(result.artifact, metadata=True)
                if type(result.artifact) is dict
                else None
                if result.artifact is None
                else safe.omit(),
            }
        elif type(result) is dict:
            state = (
                "tool_error"
                if (type(result.get("status")) is str and result["status"] == "error") or result.get("isError") is True
                else "success"
            )
            metadata = safe.value(result, metadata=True)
        elif type(result) is str:
            state = "tool_error" if result.startswith("Error") else "success"
            metadata = {"kind": "text", "characters": len(result)}
        else:
            state = "unknown"
            metadata = {"kind": safe.omit()}
        entry.update(
            result_state=state,
            result_metadata=metadata,
            duration_ms=duration_ms,
            partial=bool(safe.dropped),
            dropped_fields=safe.dropped,
        )
        size = len(json.dumps(entry, ensure_ascii=True).encode())
        with self._lock:
            if self._closed:
                return  # close already accounted for unfinished physical executions
            self._pending -= 1
            if size > _LIMITS["entry_bytes"] or self._bytes + size > _LIMITS["aggregate_bytes"]:
                self._dropped += 1
                return
            self._partial |= bool(safe.dropped)
            self._bytes += size
            self._entries.append(entry)


@contextmanager
def capture_tool_receipts():
    """Own a fresh capture scope, replacing any inherited parent collector."""
    capture = ToolReceiptCapture()
    token = _CURRENT.set(capture)
    try:
        yield capture
    except BaseException:
        with capture._lock:
            capture._partial = True
        raise
    finally:
        try:
            with capture._lock:
                capture._closed = True
                capture._dropped += capture._pending
                capture._pending = 0
        finally:
            _CURRENT.reset(token)


def mark_capture_supported():
    """Called by native middleware at agent start, even when no tool fires."""
    capture = _CURRENT.get()
    if capture is not None:
        capture._support()


@contextmanager
def observe_tool_execution(tool_call):
    """Bind ownership at handler entry; completion never resolves a new owner."""
    capture = _CURRENT.get()
    ticket = capture._begin(tool_call) if capture is not None else None
    outcome = [None, None]
    try:
        yield outcome
    except BaseException as exc:
        outcome[1] = exc
        raise
    finally:
        if ticket is not None:
            try:
                capture._finish(ticket, *outcome)
            except Exception:
                # Fail closed without replacing the handler's result/exception.
                with capture._lock:
                    if not capture._closed:
                        capture._pending -= 1
                        capture._dropped += 1


def observe_subagent_context(*, prompt, system_prompt, subagent_type, tools, fence, enforcement):
    """Observe construction once per capture, never from a reply or trace identity.

    Nested delegations cannot overwrite the owning workflow step's context.
    Text is redacted and bounded independently of native tool receipt payloads.
    """
    capture = _CURRENT.get()
    if capture is None:
        return
    with capture._lock:
        if capture._closed or capture._context_observed:
            return
        capture._context_observed = True
        callback = capture.review_context_callback
    if callback is None:
        return

    def text_record(value):
        safe = redact(value)
        return {"text": safe[:16384], "characters": len(safe), "omitted_characters": max(0, len(safe) - 16384)}

    callback({
        "status": "observed",
        "conversation": "fresh",
        "parent_history": "not_supplied",
        "inputs": {"prompt": text_record(prompt), "history": "not_supplied"},
        "instruction_sources": {
            "status": "observed",
            "subagent": subagent_type,
            "system_prompt": text_record(system_prompt),
            "external_sources": "unknown",
            "runtime_additions": "unknown",
        },
        "tool_restrictions": {
            "status": "observed",
            "bound_tools": sorted(t.name for t in tools),
            "inherited_fence": list(fence),
            "enforcement": redact(enforcement),
            "external_policy": "unknown",
        },
    })
