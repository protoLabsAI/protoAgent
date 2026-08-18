"""Chat-bundle consumption seam (#2681) — resolve an artifact reference for a bundle."""

from __future__ import annotations

import logging

from . import _store

log = logging.getLogger("protoagent.plugins.artifact")


# ── chat-bundle consumption seam (#2681) ─────────────────────────────────────
# graph.chat_bundle calls this (defensively imported — the plugin may be disabled) to
# resolve an artifact id + the version NUMBER a tool call reported back into that turn's
# actual content. Encapsulated here because only this module can correctly interpret its
# own version-chain semantics — in particular, whether a reported version number is still
# trustworthy as an index.
#
# ``_store._commit_version`` returns the POST-TRIM array length, not a lifetime-monotonic
# counter: once an artifact's version count has ever exceeded the retention cap
# (``_max_versions`` in ``_config``),
# ``_store._write_store`` truncates from the FRONT (oldest dropped), so a later commit can report
# the SAME number as an earlier one, and "version N" no longer means "index N-1". We detect
# that a trim has ever happened for this artifact structurally — its oldest surviving
# version's ``ts`` will no longer match ``art["created"]`` — rather than trying to guess
# which commit a stale number meant. No per-message timestamp exists in this graph's
# message objects to use as a tiebreaker, so once trimmed, that reference is honestly
# reported unavailable rather than risk showing the WRONG content on a public link.
def resolve_for_bundle(artifact_id: str, version: int | None) -> dict:
    """Resolve one (artifact_id, version) reference for the chat-bundle builder.

    ``version`` is 1-based, as reported in a tool call's result text; ``None`` means "the
    single version a fresh ``show_artifact`` call created" (always index 0, since nothing
    could have trimmed a version chain of length 1 yet unless a later part of the SAME
    thread pushed it past the cap — the trim check below still catches that).

    Always returns a dict; never raises. ``available=False`` covers every case a bundle
    builder must degrade gracefully for: the artifact was deleted, the reference is a
    binary `file` artifact (bytes are never bundled — a placeholder note only, per the P2
    design call), or the referenced version was trimmed away by retention.
    """
    store = _store._read_store()
    art = _store._find(store, artifact_id)
    if art is None:
        return {"id": artifact_id, "available": False, "reason": "artifact no longer exists"}

    title = art.get("title") or ""
    if _store._is_file(art):
        latest = art["versions"][-1] if art["versions"] else {}
        file_meta = latest.get("file") or {}
        return {
            "id": artifact_id,
            "kind": "file",
            "title": title,
            "available": False,
            "reason": "binary attachment not included in this export",
            "file_meta": {
                "filename": file_meta.get("filename", ""),
                "mime": file_meta.get("mime", ""),
                "size": file_meta.get("size", 0),
            },
        }

    versions = art.get("versions") or []
    # Once trimmed, EVERY version-number reference for this artifact is unreliable, not just
    # the evicted one: the reported number is `len(art["versions"])` taken AFTER that commit's
    # own trim, so once the chain sits at the cap, two DIFFERENT commits report the SAME
    # number (whatever the post-trim length pins at). "Version 2" could mean the commit that
    # first produced it (now evicted) or a later one that also landed at length 2 — there's no
    # way to tell which from the number alone, and no per-message timestamp to disambiguate
    # with. So once trimmed, give up on number-based resolution for this artifact entirely
    # rather than risk matching a reference to the WRONG revision.
    #
    # Detected via `version_count` (the lifetime total, counted before each commit's trim —
    # see `_store._commit_version`), NOT by comparing timestamps: two versions committed in the same
    # millisecond report equal `ts`, so a timestamp-equality check can false-negative on a
    # real trim. `version_count` is exact and can never collide.
    trimmed = art.get("version_count", len(versions)) > len(versions)
    idx = 0 if version is None else version - 1
    if trimmed or idx < 0 or idx >= len(versions):
        return {
            "id": artifact_id,
            "kind": art.get("kind", ""),
            "title": title,
            "available": False,
            "reason": "referenced version is no longer available (trimmed by retention)",
        }

    matched = versions[idx]
    return {
        "id": artifact_id,
        "kind": art.get("kind", ""),
        "title": title,
        "version": idx + 1,
        "available": True,
        "code": matched.get("code", ""),
        "by": matched.get("by", "agent"),
    }
