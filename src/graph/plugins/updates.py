"""Plugin/bundle update checks — ``git ls-remote`` against ``plugins.lock`` (ADR 0027/0049).

Split out of ``graph/plugins/installer.py`` (#3823). ``installer`` re-exports every
public-ish name here, so ``installer.check_plugin_update`` / ``installer.check_updates``
/ ``installer._lsremote_cache`` keep resolving for callers and tests.

**Seam rule.** The test suite patches collaborators on the ``installer`` module
(``_git``, ``_read_lock``, ``_LSREMOTE_TTL_S``, ``check_plugin_update``, …). So this
module reaches EVERY installer-owned collaborator through the installer module at call
time (``_inst()._git(...)``), never via ``from graph.plugins.installer import _git`` —
a from-import would bind the original function and silently stop honoring those
patches. ``tests/test_plugin_updates_seam.py`` guards this. ``installer`` is imported
lazily (it imports this module at its bottom, so a top-level import here would cycle).
"""

from __future__ import annotations

import os
import subprocess
import time
from types import ModuleType

# ls-remote TTL caches. Module state owned here; ``installer`` re-exports the SAME dict
# objects (tests ``.clear()`` them through ``installer``).
_lsremote_cache: dict[tuple[str, str], tuple[float, str]] = {}
# `git ls-remote --tags` cache — same TTL/timeout regime as _lsremote_cache, its own
# dict because the value is a {tag: sha} map, not a single sha.
_lstags_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _inst() -> ModuleType:
    """The installer module, resolved at call time (see the module docstring)."""
    from graph.plugins import installer

    return installer


def _ls_remote_sha(source_url: str, ref: str) -> str:
    """Latest remote commit SHA for ``ref`` (or the default branch / HEAD when
    ``ref`` is empty) at ``source_url``, via ``git ls-remote``. TTL-cached per
    (source_url, ref) and bounded by a short timeout so the UI poll can't hang.

    Raises ``InstallError`` (git failure) or ``subprocess.TimeoutExpired`` — both
    treated as a non-fatal per-plugin error by ``check_updates``."""
    inst = _inst()
    key = (source_url, ref or "")
    now = time.monotonic()
    hit = _lsremote_cache.get(key)
    if hit is not None and (now - hit[0]) < inst._LSREMOTE_TTL_S:
        return hit[1]

    # `git ls-remote <url> <ref>` prints "<sha>\t<refname>" lines; with no ref it
    # lists everything and we take HEAD. We always pass an explicit refspec when we
    # have one (branch/tag), else "HEAD". For an ANNOTATED tag the bare refspec
    # returns the tag-object SHA — never equal to the lock's commit SHA, so a naive
    # compare reports a permanent false "behind" (ADR 0049). Ask for the peeled
    # `<ref>^{}` too and prefer it; branches/HEAD/lightweight tags simply don't
    # match the peeled refspec and fall back to the bare line.
    refspecs = [ref, ref + "^{}"] if ref else ["HEAD"]
    # Authenticate the update-check for a PRIVATE github repo (#1805 parity — that fix covered
    # the clone/install path; a plain `git ls-remote` of a private repo still failed auth here,
    # surfacing as "check failed" in the plugins panel). Scoped/off-argv/off-disk via GIT_CONFIG_*.
    _auth = inst._git_auth_env(source_url)
    out = inst._git(
        "ls-remote",
        source_url,
        *refspecs,
        timeout=inst._LSREMOTE_TIMEOUT_S,
        env={**os.environ, **_auth} if _auth else None,
    )
    sha = peeled = ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].strip():
            continue
        if parts[1].strip().endswith("^{}"):
            peeled = peeled or parts[0].strip()
        else:
            sha = sha or parts[0].strip()
    sha = peeled or sha
    _lsremote_cache[key] = (now, sha)
    return sha


def _ls_remote_tags(source_url: str) -> dict[str, str]:
    """``{tag: commit_sha}`` for every tag at ``source_url`` (TTL-cached, one
    timeout-bounded ``ls-remote --tags``). An annotated tag lists twice — the bare
    ref (tag-object SHA) and the peeled ``<ref>^{}`` (commit SHA); the peeled one
    wins, mirroring _ls_remote_sha's compare semantics (ADR 0049)."""
    inst = _inst()
    now = time.monotonic()
    hit = _lstags_cache.get(source_url)
    if hit is not None and (now - hit[0]) < inst._LSREMOTE_TTL_S:
        return hit[1]
    _auth = inst._git_auth_env(source_url)  # authenticate the update-check for a private github repo (#1805 parity)
    out = inst._git(
        "ls-remote",
        "--tags",
        source_url,
        timeout=inst._LSREMOTE_TIMEOUT_S,
        env={**os.environ, **_auth} if _auth else None,
    )
    tags: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].strip():
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/") :]
        if name.endswith("^{}"):
            tags[name[:-3]] = sha  # peeled commit — overwrite the tag-object sha
        else:
            tags.setdefault(name, sha)
    _lstags_cache[source_url] = (now, tags)
    return tags


def check_plugin_update(entry: dict) -> dict:
    """Update status for one ``plugins.lock`` entry. A *pinned* plugin (its
    ``requested_ref`` is a full/abbrev commit SHA per ``_SHA_RE``) never
    auto-updates — we skip the network call entirely. A RELEASE-TAG ref
    (``vX.Y.Z``) is immutable, so "behind" there means a NEWER semver tag exists
    on the remote (reported as ``latest_ref`` — the pin-lifecycle signal, ADR
    0049) — with a moved-tag compare as the fallback. Any other ref compares the
    stored ``resolved_sha`` against the latest remote SHA. Any network/timeout/
    lookup failure is reported in ``error`` (non-fatal)."""
    inst = _inst()
    pid = entry.get("id", "")
    source_url = entry.get("source_url", "")
    requested_ref = entry.get("requested_ref", "") or ""
    current_sha = entry.get("resolved_sha", "") or ""

    pinned = bool(inst._SHA_RE.match(requested_ref))
    result = {
        "id": pid,
        "source_url": source_url,
        "requested_ref": requested_ref,
        "current_sha": current_sha,
        "latest_sha": None,
        "latest_ref": None,
        "behind": False,
        "pinned": pinned,
        "error": None,
    }
    # Superseded by a bundled copy: the installed copy is ignored, so "behind" would
    # offer an update that can't apply — report it, skip the network.
    bundled = inst.bundled_superseding(str(pid), str(source_url))
    if bundled is not None:
        result["superseded"] = True  # the bundled version rides the inventory row, not here
        return result
    if pinned or not source_url:
        if not source_url:
            result["error"] = "no source_url recorded — cannot check for updates"
        return result

    tag_key = inst._semver_key(requested_ref)
    try:
        if tag_key is not None:
            tags = inst._ls_remote_tags(source_url)
            newest_key, newest = tag_key, None
            for name, sha in tags.items():
                k = inst._semver_key(name)
                if k is not None and k > newest_key:
                    newest_key, newest = k, (name, sha)
            if newest is not None:
                result["latest_ref"], result["latest_sha"] = newest
                result["behind"] = True
                return result
            # No newer release — fall through to the moved-tag compare: the SAME
            # tag re-pointed at a different commit still counts as behind.
            latest = tags.get(requested_ref, "")
        else:
            latest = inst._ls_remote_sha(source_url, requested_ref)
    except subprocess.TimeoutExpired:
        result["error"] = f"ls-remote timed out after {inst._LSREMOTE_TIMEOUT_S:.0f}s"
        return result
    except inst.InstallError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 — update check must never be fatal
        result["error"] = str(exc)
        return result

    if not latest:
        result["error"] = "could not resolve a remote SHA for the ref"
        return result
    result["latest_sha"] = latest
    # current_sha is a full 40-char SHA from the lock; ls-remote returns full SHAs
    # too, so a plain (case-insensitive) inequality is the behind signal.
    result["behind"] = bool(current_sha) and latest.lower() != current_sha.lower()
    return result


def check_updates() -> list[dict]:
    """Per-plugin update status for every locked plugin (see ``check_plugin_update``).
    Pinned-to-SHA plugins skip the network; the rest ls-remote their ref (TTL-cached,
    timeout-bounded) and report ``behind``. Network errors are non-fatal per entry.

    One row per id (``_lock_rows_by_id``), like every other reader: a lock that lists an
    id twice used to yield two update rows for one plugin — and the row the loader does
    NOT use could report an update the operator can't apply."""
    inst = _inst()
    return [inst.check_plugin_update(e) for e in inst._lock_rows_by_id().values()]


def check_bundle_updates() -> list[dict]:
    """Bundle-level update status (ADR 0049 D4, #2718). A bundle lock row carries the
    same ``{id, source_url, requested_ref, resolved_sha}`` shape as a plugin row, so each
    rides ``check_plugin_update`` unchanged — ``behind`` means the bundle REPO moved
    (its member pins may have moved with it; ``ops.plugins.update_bundle``
    re-resolves them). Same pinned/release-tag/TTL semantics as plugins."""
    inst = _inst()
    return [inst.check_plugin_update(b) for b in inst._read_lock().get("bundles") or []]
