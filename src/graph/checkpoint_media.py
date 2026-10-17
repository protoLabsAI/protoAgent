"""Media offload for the SQLite checkpointer (ADR 0120).

``SqliteSaver`` stores a checkpoint's whole ``channel_values`` inline, once per
super-step. A thread that has seen images (screenshots, rendered stills, page crops)
re-serializes every one of them into every checkpoint row: a long visual-review turn
on brandLaunch wrote ~2,200 checkpoints of ~55 MB each (~56 MB of it base64 images
from tool results) — 87 GB in five hours.

This module moves those payloads out of the per-checkpoint row:

* :func:`offload_value` walks message content blocks and swaps every large base64
  / ``data:`` string for a short reference (``protoagent-ckblob:sha256:<hex>``),
  returning the payloads it lifted. It never mutates its input: changed messages are
  ``model_copy``'d, untouched ones are passed through by identity.
* The saver stores each payload ONCE per thread in ``checkpoint_blobs``
  (content-addressed, so the same screenshot in 300 checkpoints costs one row).
* :func:`rehydrate_value` restores the exact strings on read, so the graph, the
  model and the console see byte-identical messages. A reference whose payload is
  gone degrades to a text block instead of a broken image.

Only content blocks are touched (``message.content`` when it is a list) — that is
where images live in every provider shape LangChain carries (OpenAI ``image_url``
data URIs, Anthropic ``source.data``, v1 ``base64`` / ``data``). Plain string content
and tool-call args are left alone.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any, Callable

#: Strings at least this long (chars) that look like base64 / a data URI are offloaded.
MIN_OFFLOAD_CHARS = 32 * 1024

REF_PREFIX = "protoagent-ckblob:sha256:"
_REF_RE = re.compile(r"^protoagent-ckblob:sha256:([0-9a-f]{64})$")
#: The same pattern over raw serialized bytes — how the blob GC finds live references.
REF_BYTES_RE = re.compile(rb"protoagent-ckblob:sha256:([0-9a-f]{64})")

_B64_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_")

BLOB_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS checkpoint_blobs ("
    " thread_id TEXT NOT NULL,"
    " hash TEXT NOT NULL,"
    " data BLOB NOT NULL,"
    " created REAL NOT NULL DEFAULT 0,"
    " PRIMARY KEY (thread_id, hash))"
)

UNAVAILABLE_TEXT = "[image no longer available: its checkpoint payload was removed]"


def _looks_like_media(s: str) -> bool:
    if len(s) < MIN_OFFLOAD_CHARS:
        return False
    if s.startswith("data:"):
        return ";base64," in s[:200]
    head = s[:256].encode("ascii", "ignore")
    return len(head) == min(256, len(s)) and all(c in _B64_CHARS for c in head)


class _HashCache:
    """Identity cache of sha256 digests, bounded by total chars held. The same image
    string object rides in every checkpoint of a thread; hashing it once instead of on
    every super-step keeps ``put`` cheap."""

    def __init__(self, budget_chars: int = 128 * 1024 * 1024) -> None:
        self._budget = budget_chars
        self._held = 0
        self._d: OrderedDict[int, tuple[str, str]] = OrderedDict()

    def digest(self, s: str) -> str:
        hit = self._d.get(id(s))
        if hit is not None and hit[0] is s:
            self._d.move_to_end(id(s))
            return hit[1]
        h = hashlib.sha256(s.encode("utf-8")).hexdigest()
        self._d[id(s)] = (s, h)
        self._held += len(s)
        while self._held > self._budget and self._d:
            _, (old, _h) = self._d.popitem(last=False)
            self._held -= len(old)
        return h


_CACHE = _HashCache()


def _is_message(v: Any) -> bool:
    try:
        from langchain_core.messages import BaseMessage
    except Exception:  # pragma: no cover - langchain is a hard dep
        return False
    return isinstance(v, BaseMessage)


#: Keys whose strings are prose, never media — not offloaded even when long and
#: base64-shaped (a run of letters is valid base64).
_PROSE_KEYS = frozenset({"text", "thinking", "reasoning"})


def _map_block(block: Any, fn: Callable[[str], str | None], skip: frozenset = frozenset()) -> tuple[Any, bool]:
    """Apply ``fn`` to every string leaf of a block (except under ``skip`` keys).
    ``fn`` returns a replacement or ``None`` (keep). Returns (new_block, changed);
    unchanged sub-trees keep identity."""
    if isinstance(block, str):
        rep = fn(block)
        return (rep, True) if rep is not None else (block, False)
    if isinstance(block, dict):
        out, changed = None, False
        for k, v in block.items():
            if k in skip:
                continue
            nv, c = _map_block(v, fn, skip)
            if c:
                if out is None:
                    out = dict(block)
                out[k] = nv
                changed = True
        return (out if changed else block), changed
    if isinstance(block, list):
        out_l, changed = None, False
        for i, v in enumerate(block):
            nv, c = _map_block(v, fn, skip)
            if c:
                if out_l is None:
                    out_l = list(block)
                out_l[i] = nv
                changed = True
        return (out_l if changed else block), changed
    return block, False


def _map_message(m: Any, block_fn: Callable[[Any], tuple[Any, bool]]) -> Any:
    content = getattr(m, "content", None)
    if not isinstance(content, list):
        return m
    new_content, changed = None, False
    for i, b in enumerate(content):
        nb, c = block_fn(b)
        if c:
            if new_content is None:
                new_content = list(content)
            new_content[i] = nb
            changed = True
    return m.model_copy(update={"content": new_content}) if changed else m


def _map_messages(value: Any, block_fn: Callable[[Any], tuple[Any, bool]]) -> Any:
    """A message, or a list/tuple holding messages, mapped; anything else unchanged."""
    if _is_message(value):
        return _map_message(value, block_fn)
    if isinstance(value, (list, tuple)) and any(_is_message(x) for x in value):
        mapped = [(_map_message(x, block_fn) if _is_message(x) else x) for x in value]
        if all(a is b for a, b in zip(mapped, value)):
            return value
        return type(value)(mapped) if isinstance(value, tuple) else mapped
    return value


def offload_value(value: Any, blobs: dict[str, str]) -> Any:
    """``value`` with large media strings in message content blocks replaced by
    references; each lifted payload is added to ``blobs`` (hash → string)."""

    def lift(s: str) -> str | None:
        if not _looks_like_media(s):
            return None
        h = _CACHE.digest(s)
        blobs[h] = s
        return REF_PREFIX + h

    return _map_messages(value, lambda b: _map_block(b, lift, _PROSE_KEYS))


def offload_channel_values(channel_values: dict, blobs: dict[str, str]) -> dict:
    """Every channel mapped through :func:`offload_value` (a new dict only on change)."""
    out = None
    for k, v in channel_values.items():
        nv = offload_value(v, blobs)
        if nv is not v:
            if out is None:
                out = dict(channel_values)
            out[k] = nv
    return out if out is not None else channel_values


def refs_in_value(value: Any, found: set[str]) -> None:
    """Collect every reference hash in a (deserialized) value's message blocks."""

    def note(s: str) -> str | None:
        m = _REF_RE.match(s)
        if m:
            found.add(m.group(1))
        return None

    _map_messages(value, lambda b: _map_block(b, note))


def rehydrate_value(value: Any, payloads: dict[str, str]) -> Any:
    """Inverse of :func:`offload_value`. A block holding a reference with no payload
    becomes a text block (a dangling image would fail the next model call)."""

    def block_fn(block: Any) -> tuple[Any, bool]:
        missing = False

        def restore(s: str) -> str | None:
            nonlocal missing
            m = _REF_RE.match(s)
            if not m:
                return None
            data = payloads.get(m.group(1))
            if data is None:
                missing = True
                return s
            return data

        nb, changed = _map_block(block, restore)
        if missing:
            return {"type": "text", "text": UNAVAILABLE_TEXT}, True
        return nb, changed

    return _map_messages(value, block_fn)
