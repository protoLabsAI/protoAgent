"""Install plugins from a git URL (ADR 0027).

Fetches a plugin repo into the **live** plugins dir (``<config_dir>/plugins/<id>``,
the one ``loader._plugin_roots`` already discovers), pinned to a resolved commit
SHA and recorded in a committed ``plugins.lock`` for reproducibility.

Safety model (ADR 0027): **install ≠ enable ≠ trust**. This module only puts code
on disk + reads the manifest (data) — it never imports the plugin and never
pip-installs its deps (``requires_pip`` is declared, installed explicitly later).
Enabling (``plugins.enabled`` → ``register()``) is the separate trust decision.
For *untrusted* code use MCP (out-of-process), not a git plugin.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from infra.paths import instance_paths

from graph.plugins.manifest import MANIFEST_FILENAME, PluginManifest, is_swap_leftover, load_manifest

log = logging.getLogger(__name__)


def bundled_plugins_dir() -> Path:
    """In-tree bundled (built-in) plugins root — ``app_root/plugins``. Resolved at
    call time so the env (PyInstaller _MEIPASS) is honored, never import-time."""
    return instance_paths().app_root / "plugins"


def _is_builtin(plugin_id: str) -> bool:
    """True iff ``plugins/<id>`` is a REAL bundled plugin or bundle — i.e. it holds
    a manifest (``protoagent.plugin.yaml``) or bundle file (``protoagent.bundle.yaml``),
    mirroring how the loader (:func:`load_manifest`) decides a directory is a plugin.

    A bare directory is NOT a built-in: a ``__pycache__``-only leftover from a
    core→standalone extraction (git doesn't track it, so it survives on every
    machine that ever imported the old plugin) must not block installing or
    uninstalling the standalone successor of the same id (#1731).

    Also true for any id a bundled manifest DECLARES, whatever its folder is called — the
    loader keys plugins by manifest id, so the guard has to as well."""
    d = bundled_plugins_dir() / plugin_id
    return (d / MANIFEST_FILENAME).exists() or (d / BUNDLE_FILENAME).exists() or plugin_id in _bundled_index()


# ── Supersession: an external plugin that moved into core (``supersedes``) ─────────
# A bundled plugin can declare the git URLs of the standalone repo(s) it replaces
# (PluginManifest.supersedes). A URL listed there is "superseded": installing from it
# fetches nothing (the plugin already ships with protoAgent), updating a copy installed
# from it is refused with a clear reason instead of a 400 from the built-in guard, and
# uninstalling that copy removes only the ignored files — never the enable state the
# bundled copy runs on. The loader's half (the bundled copy wins) is in
# ``loader.discover_plugins``.
#
# "Which copy of <id> runs?" has ONE answer, the loader's: every question here goes
# through ``_bundled_index`` (bundled copies keyed by MANIFEST id, via the loader's own
# ``discover_plugins``), ``_lock_entry`` (one lock row per id, the same one the loader
# reads) and ``effective_copies`` (the loader's full precedence rule).

# bundled-tree path → (manifest stamps, {id: manifest}). Re-read when any manifest changes.
_BUNDLED_INDEX_CACHE: dict[str, tuple[tuple, dict[str, PluginManifest]]] = {}


def _bundled_index() -> dict[str, PluginManifest]:
    """``{plugin id: bundled copy}`` for the in-tree ``plugins/`` tree — keyed by MANIFEST
    id exactly as the loader keys it, so a folder named ``agent-browser`` holding id
    ``agent_browser`` is found under ``agent_browser`` here too. Cached per tree; the
    cache key is every manifest's name + CONTENT (``_content_stamp``), so an edit is picked
    up at once on every filesystem. Measured on the real tree (13 manifests, ~30 KB):
    hashing adds ~0.25 ms to a ~0.55 ms lookup, against a ~26 ms re-parse on a miss. A
    stat-only key was cheaper, but it missed a same-size rewrite inside one mtime tick,
    and on Windows every such rewrite."""
    root = bundled_plugins_dir()
    try:
        children = sorted(c for c in root.iterdir() if (c / MANIFEST_FILENAME).is_file())
        stamp = tuple((c.name, *_content_stamp((c / MANIFEST_FILENAME).read_bytes())) for c in children)
    except OSError:
        return {}
    hit = _BUNDLED_INDEX_CACHE.get(str(root))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    from graph.plugins.loader import discover_plugins

    index = {m.id: m for m in discover_plugins([root], tracked_sources={})}
    if len(_BUNDLED_INDEX_CACHE) > 8:
        _BUNDLED_INDEX_CACHE.clear()  # one root per process in the field; a test suite makes many
    _BUNDLED_INDEX_CACHE[str(root)] = (stamp, index)
    return index


def _bundled_manifest(plugin_id: str) -> PluginManifest | None:
    """The bundled copy of ``plugin_id``, or ``None`` if protoAgent doesn't ship one."""
    return _bundled_index().get(plugin_id)


def bundled_superseding(plugin_id: str, source_url: str) -> PluginManifest | None:
    """The bundled copy of ``plugin_id`` when it supersedes ``source_url`` — i.e. a copy
    of that id installed from that URL is retired — else ``None``."""
    if not source_url:
        return None
    from graph.plugins.manifest import supersedes_source

    bundled = _bundled_manifest(plugin_id)
    return bundled if bundled is not None and supersedes_source(bundled, source_url) else None


def superseding_plugin(url: str) -> PluginManifest | None:
    """The bundled plugin whose ``supersedes`` names ``url``, or ``None``.

    Answered from the bundled tree alone — no fetch — so an install or an archetype
    member listing a retired repo resolves even when that repo is archived, deleted,
    or unreachable."""
    from graph.plugins.manifest import supersedes_source

    return next((m for m in _bundled_index().values() if m.supersedes and supersedes_source(m, url)), None)


def _content_stamp(data: bytes) -> tuple[int, bytes]:
    """A cache key from a file's CONTENT: its length and a 128-bit BLAKE2b digest.

    A stat-based key (mtime, size, inode, ctime) can't see every edit. mtime is coarse on
    HFS+ (1 s), FAT/exFAT (2 s) and some network mounts. On Windows ``st_ctime`` is the
    CREATION time, and an in-place rewrite keeps the NTFS file index. So a same-size save
    inside one tick left every stat field identical, and the cache served the old value
    until a restart (#3455 Windows CI). Hashing the bytes costs ~50 µs for a 30 KB config,
    against the ~20 ms YAML parse the cache exists to skip."""
    return (len(data), hashlib.blake2b(data, digest_size=16).digest())


# config path → ((size, digest), override). `live_plugins_dir()` resolves the override on
# every call and a plugins request makes several, so parsing the YAML each time is real
# work: 11.7 ms per call against a 30 KB config here, ~6 calls on GET /api/plugins/installed.
_PLUGINS_DIR_CACHE: dict[str, tuple[tuple[int, bytes], str]] = {}


def configured_plugins_dir() -> str:
    """``plugins.dir`` from the live config file — the operator's override of the live
    plugins root — read without a config object (the ``configured_allowlist`` pattern).
    ``""`` when unset, unreadable, or refused (a relative value). Cached per config file
    on its content, so any edit is picked up at once, on every filesystem."""
    try:
        from graph.config_io import config_yaml_path

        cfg_path = config_yaml_path()
        try:
            raw = cfg_path.read_bytes()
        except OSError:
            return ""  # no live config yet — the instance default applies
        # Keyed on the bytes, not the file's stat (see `_content_stamp`); the same bytes are
        # what gets parsed on a miss, so the key can never describe a different read.
        stamp = _content_stamp(raw)
        hit = _PLUGINS_DIR_CACHE.get(str(cfg_path))
        if hit is not None and hit[0] == stamp:
            return hit[1]
        import yaml

        from graph.plugins.pconfig import valid_plugins_dir_override

        data = yaml.safe_load(raw) or {}
        # Vetted by the one shared validator (a relative value is refused, with a reason)
        # so the file read and the config-object read can't disagree.
        value = valid_plugins_dir_override((data.get("plugins") or {}).get("dir"))
        if len(_PLUGINS_DIR_CACHE) > 8:
            _PLUGINS_DIR_CACHE.clear()  # one config per process in the field; a suite makes many
        _PLUGINS_DIR_CACHE[str(cfg_path)] = (stamp, value)
        return value
    except Exception:  # noqa: BLE001 — a config read must never break resolution
        return ""


def loader_roots() -> list[Path]:
    """The roots the LOADER discovers, in its order: bundled tree first, then the live
    plugins dir — the same pair as ``loader._plugin_roots`` and
    ``pconfig.plugin_roots_from``, including the ``plugins.dir`` override that
    ``live_plugins_dir`` resolves.

    Public because the loader itself needs it where it has no config object (the frozen
    ``--mcp-plugin`` shim): the installer owns where installed copies live, so it answers
    the question rather than a third copy of the pair drifting from the other two."""
    return [bundled_plugins_dir(), live_plugins_dir()]


def _same_dir(a: Path, b: Path) -> bool:
    """Same directory, as spelled or after following symlinks (either match counts, so a
    live root symlinked at the bundled tree is recognised as the same place)."""
    forms = []
    for p in (a, b):
        variants = {os.path.normcase(os.path.abspath(str(p)))}
        try:
            variants.add(os.path.normcase(str(Path(p).resolve())))
        except OSError:
            pass
        forms.append(variants)
    return bool(forms[0] & forms[1])


def effective_copies() -> dict[str, PluginManifest]:
    """``{plugin id: the copy the loader runs}`` across the loader's own roots — computed
    by the loader's own ``discover_plugins``, so ``supersedes``, tracked overrides and the
    #1574 untracked rule all apply exactly as at load. Use it for anything that acts on
    "the plugin" rather than on a particular folder: its deps, its description, its
    version."""
    from graph.plugins.loader import discover_plugins

    return {m.id: m for m in discover_plugins(loader_roots())}


def effective_source_url(plugin_id: str) -> str:
    """Where the copy that RUNS came from: ``""`` when that is the bundled copy — nothing
    was fetched for it, so there is no source to re-check or ask consent for, even if an
    ignored, superseded git copy is still recorded — else the ``plugins.lock`` origin
    (``""`` for an untracked folder).

    Fails closed in every ambiguous case, because this waives a consent/allowlist gate:
    an id the resolver can't place keeps its recorded origin, and a copy is only "the
    bundled one" when it sits in the bundled tree AND that tree is not itself the live
    plugins root (with ``plugins.dir`` or ``PROTOAGENT_PLUGINS_DIR`` aimed at the app's
    own tree, a git-installed plugin lands there too, and a parent-dir comparison alone
    would call it bundled)."""
    running = effective_copies().get(plugin_id)
    if running is not None:
        bundled_root, live_root = loader_roots()
        if _same_dir(running.path.parent, bundled_root) and not _same_dir(bundled_root, live_root):
            return ""
    return recorded_source_url(plugin_id)


def superseded_reason(plugin_id: str, source_url: str, bundled: PluginManifest) -> str:
    """One sentence an operator can act on: why a copy is ignored and what to do."""
    from graph.plugins.manifest import display_source

    return (
        f"{plugin_id!r} now ships with protoAgent (bundled v{bundled.version}), which supersedes "
        f"{display_source(source_url)} — the copy installed from there is ignored and has nothing to "
        f"update. The bundled copy updates with protoAgent itself; uninstall the old copy to clean "
        f"it up (your settings and enabled state are kept)."
    )


def lock_path() -> Path:
    """The ``plugins.lock`` for THIS instance — ``instance_paths().plugins_lock``
    (honors ``PROTOAGENT_PLUGINS_LOCK``)."""
    return instance_paths().plugins_lock


_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
# A git ref we'll accept from a caller — branch/tag/sha shapes only. Keeps a ref
# from being interpolated into the GitHub API URL (path/query injection) or passed
# to git as an option (a leading `-`). Permissive enough for real refs
# (`release/1.2`, `v1.0.0`, a 40-char sha) but no `..`, control chars, or schemes.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_ALLOWED_SCHEMES = ("https://", "http://", "git://", "ssh://", "git@", "file://", "/")

# `git ls-remote` network safety (check_updates): a bounded timeout so a slow/dead
# remote can't hang the UI poll, and a small module-level TTL cache keyed by
# (source_url, ref) so repeated polls don't ls-remote the same source every call.
_LSREMOTE_TIMEOUT_S = 5.0
_LSREMOTE_TTL_S = 300.0  # ~5 min
# A git clone of a slow/large remote is bounded so it can't hang an install thread
# indefinitely (the operator install/update routes offload to a thread, so this
# caps the worst case rather than wedging a pool worker forever).
_CLONE_TIMEOUT_S = 600.0
_lsremote_cache: dict[tuple[str, str], tuple[float, str]] = {}


class InstallError(RuntimeError):
    """A plugin install/uninstall/sync failed (bad URL, manifest, git, collision)."""


class BundleNotInstalledError(InstallError):
    """The named bundle has no ``plugins.lock`` row — typed so HTTP adapters can map
    "doesn't exist" to 404 without string-matching. Raised by the op itself, so the
    mapping stays correct even when a concurrent DELETE removes the bundle just
    before the op runs (there is no route-level pre-check to race)."""


def live_plugins_dir() -> Path:
    """Where git-installed plugins live — the live dir the LOADER discovers: the
    ``plugins.dir`` config override when set, else ``instance_paths().plugins_dir``
    (honoring ``PROTOAGENT_PLUGINS_DIR``).

    The override is read HERE, not only in ``loader._plugin_roots``, so every lifecycle
    operation acts on the dir the loader actually reads — install, uninstall (a
    superseded copy's removal included), the installed inventory, sync, and scaffolding.
    Honoring it in only some of those is how a removal drops a lock row while leaving the
    copy on disk: the copy then loads as an UNTRACKED one and can shadow the bundled
    plugin again under the #1574 rule."""
    override = configured_plugins_dir()
    return Path(override).expanduser() if override else instance_paths().plugins_dir


def _git(*args: str, cwd: Path | None = None, timeout: float | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,  # None ⇒ inherit os.environ (default); a dict carries scoped auth (see _git_auth_env)
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise InstallError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _validate_url(url: str) -> None:
    if any(url.startswith(s) for s in _ALLOWED_SCHEMES):
        return
    if re.match(r"^[A-Za-z]:[\\/]", url):
        return  # a Windows drive-absolute local path (C:\src\my-plugin) — the "/" entry's sibling
    raise InstallError(f"unsupported source {url!r} — use https://, ssh://, git@, or a local path.")


def _validate_ref(ref: str) -> None:
    """Reject a ref that could escape the GitHub API URL path / inject a query, or
    reach git as an option. Empty = the default branch (resolved separately)."""
    if ".." in ref or not _REF_RE.match(ref):
        raise InstallError(f"invalid ref {ref!r} — use a branch, tag, or commit SHA.")


def _source_allowed(url: str, allow: list[str] | None) -> bool:
    """Optional fork lock-down (ADR 0027 D3): if an allowlist is configured, the
    URL must match one of its host/org globs (e.g. ``github.com/protoLabsAI/*``).

    The predicate is ``trust.source_matches`` — ONE function shared with the trust
    matcher (they drifted byte-for-byte twice; the 2739 panel asked for one home):
    path-boundary widening (never bare ``pat*``), both sides normalized, with the
    ``.git`` trim applied only to glob-free entries (a glob's ``.git`` suffix is
    semantics, not spelling)."""
    if allow is None:
        return True  # key absent — open (the documented default)
    if not allow:
        return False  # EXPLICIT empty list — deny-all (#2743 item 1)
    from graph.plugins.trust import source_matches

    return source_matches(url, allow)


def _normalize_lock(data: object) -> dict:
    """Return the canonical lock shape and absorb the legacy wheel-deps layout.

    ADR 0027 owns ``plugins.lock`` as a top-level ``plugins`` list. The original
    ADR 0093 writer instead added ``{plugin_id: {deps: [...]}}`` beside that list.
    Normalize those released entries in memory so the next ordinary write
    persists one schema without dropping dependency pins.
    """
    if not isinstance(data, dict):
        return {"plugins": []}
    normalized = dict(data)
    plugins = normalized.get("plugins")
    if not isinstance(plugins, list):
        plugins = []
        normalized["plugins"] = plugins

    by_id = {
        plugin_id: e for e in plugins if isinstance(e, dict) and isinstance(plugin_id := e.get("id"), str) and plugin_id
    }
    for key, value in list(normalized.items()):
        if key in {"plugins", "bundles"} or not isinstance(value, dict) or not isinstance(value.get("deps"), list):
            continue
        entry = by_id.get(key)
        if entry is None:
            entry = {"id": key}
            plugins.append(entry)
            by_id[key] = entry
        entry["deps"] = value["deps"]
        del normalized[key]
    return normalized


def _read_lock() -> dict:
    lock = lock_path()
    if lock.exists():
        try:
            return _normalize_lock(json.loads(lock.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("[plugins] %s is unreadable — starting a fresh lock", lock)
    return {"plugins": []}


def _lock_rows_by_id(lock: dict | None = None) -> dict[str, dict]:
    """``{plugin id: its plugins.lock row}`` — THE row choice for every reader. An install
    rewrites one row per id, but a hand-edited or merged lock can carry two; the LAST one
    wins (the loader, the inventory and uninstall must agree on which copy is recorded —
    reading different rows let the banner say "uninstall" while uninstall refused, or
    deleted a live fork override as "superseded")."""
    rows: dict[str, dict] = {}
    for e in (lock if lock is not None else _read_lock()).get("plugins") or []:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]:
            rows[e["id"]] = e
    return rows


def _lock_entry(plugin_id: str) -> dict | None:
    """``plugin_id``'s ``plugins.lock`` row (see ``_lock_rows_by_id``), or ``None``."""
    return _lock_rows_by_id().get(plugin_id)


def _write_lock(data: dict) -> None:
    data = _normalize_lock(data)
    data["plugins"].sort(key=lambda e: e.get("id", "") if isinstance(e, dict) and isinstance(e.get("id"), str) else "")
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Atomic + concurrency-safe: a crash mid-write must not leave a truncated lock
    # that _read_lock treats as FRESH (silently dropping every pin), and each writer
    # stages to its OWN temp file — a shared ".tmp" path let two concurrent writes
    # interleave into a corrupt replace (coderabbit on the 2739 thread).
    fd, tmp_name = tempfile.mkstemp(dir=lock.parent, prefix=lock.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, lock)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _audit(action: str, args: dict, summary: str, *, success: bool = True) -> None:
    """Record install/uninstall/install-deps to the audit log (ADR 0027 D5)."""
    try:
        from observability.audit import audit_logger

        audit_logger.log(
            session_id="plugins",
            tool=f"plugin.{action}",
            args=args,
            result_summary=summary,
            duration_ms=0,
            success=success,
        )
    except Exception:  # noqa: BLE001 — auditing must never block the operation
        log.debug("[plugins] audit log failed for %s", action, exc_info=True)


def _allow_unbundled_deps() -> bool:
    """`plugins.allow_unbundled_deps` read from the live config file (ADR 0093 opt-in).
    Read here rather than threaded through ``install_deps`` — the CLI + the route both
    call it without a loaded LangGraphConfig. Default off; any read error = off."""
    try:
        import yaml

        from graph.config_io import config_yaml_path

        cfg_path = config_yaml_path()
        if not cfg_path.exists():
            return False
        data = yaml.safe_load(cfg_path.read_text()) or {}
        return bool((data.get("plugins") or {}).get("allow_unbundled_deps", False))
    except Exception:  # noqa: BLE001 — a config read must never flip the gate ON by accident
        return False


def configured_allowlist() -> list[str] | None:
    """`plugins.sources.allow` read from the live config file (for the CLI, which
    runs without a loaded LangGraphConfig). ``None`` = key absent (open);
    ``[]`` = explicit deny-all (#2743 item 1); non-empty = the allowlist."""
    try:
        import yaml

        from graph.config_io import config_yaml_path

        cfg_path = config_yaml_path()
        if not cfg_path.exists():
            return None
        data = yaml.safe_load(cfg_path.read_text()) or {}
        sources = ((data.get("plugins") or {}).get("sources")) or {}
        if "allow" not in sources:
            return None  # key absent — open
        # Explicit [] survives as [] (deny-all, #2743 item 1) — the old `or None`
        # collapsed it back to open, exactly the ambiguity this distinction removes.
        return [str(x) for x in (sources.get("allow") or [])]
    except Exception:  # noqa: BLE001
        return None


def _summary(m: PluginManifest, *, source: str, ref: str, sha: str) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "version": m.version,
        "description": m.description,
        "source_url": source,
        "requested_ref": ref,
        "resolved_sha": sha,
        "repository": m.repository,
        "homepage": m.homepage,
        "capabilities": m.capabilities,
        "requires_env": m.requires_env,
        "requires_pip": m.requires_pip,
        "optional_pip": m.optional_pip,
        "min_protoagent_version": m.min_protoagent_version,
        # what it contributes — surfaced in the install review (ADR 0027 D3)
        "contributes": {
            "tools": bool(m.config_section),  # heuristic; real tool list needs import
            "views": [v.get("label") for v in m.views],
            "secrets": m.secrets,
            "settings": [s.get("key") for s in m.settings],
        },
    }


def _git_auth_env(url: str) -> dict[str, str]:
    """Env that hands ``git`` a GitHub auth header for an ``https://github.com/`` URL when a
    ``GITHUB_TOKEN`` / ``GH_TOKEN`` is set — so a runtime ``plugin install`` of a **private**
    repo works on the DEFAULT git path (the "git handles private auth" design intent), not only
    the archive path. Without it a plain ``git clone`` of a private HTTPS repo in a container
    (no ssh key, no credential helper — just the token env) fails with
    ``could not read Username for 'https://github.com'``.

    Delivered via ``GIT_CONFIG_*`` env, deliberately:
    - **not argv** → the token never shows up in ``ps`` (unlike ``-c http.extraheader=…``),
    - **not the clone's ``.git/config``** → env-config is per-command, so the token never lands
      on disk in the cloned repo,
    - **scoped** to ``http.https://github.com/.extraheader`` → the header never rides a redirect
      to a non-github host.

    SSH / ``git@`` / non-github / no-token → ``{}`` (git's own auth — ssh keys, credential
    helpers — applies unchanged)."""
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token or not url.startswith("https://github.com/"):
        return {}
    import base64

    # GitHub accepts Basic ``x-access-token:<token>`` for git-over-HTTPS (the pattern
    # actions/checkout uses); the archive path uses a Bearer header for the API/codeload.
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _clone(url: str, ref: str | None, dest: Path) -> str:
    """Clone ``url`` at ``ref`` into ``dest``; return the resolved commit SHA.

    A plain LOCAL path is rewritten to a ``file://`` URL first. Without it git
    uses its "local transport" — a readdir+copy of the source's ``.git/objects``
    — which silently ignores ``--depth`` and races anything concurrently writing
    the source repo: ``git commit`` spawns a DETACHED ``git maintenance run
    --auto``, and on CI that background maintenance renamed a ``tmp_rev_*`` pack
    file mid-copy → ``fatal: failed to copy file … No such file or directory``
    (#1600). ``file://`` forces the regular pack transport (upload-pack streams a
    fresh pack; nothing enumerates the source's raw object files), which is
    immune to that race — exactly what git's own warning recommends.
    ``--no-hardlinks`` stays as the belt for any local-transport clone that
    slips through (it avoids the older ``hardlink different from source`` race);
    it's a no-op for the file:// and remote cases."""
    if url.startswith("/"):
        url = Path(url).resolve().as_uri()
    # Authenticate a private github HTTPS clone with the token env (scoped, off-argv, off-disk).
    # ``None`` for local/ssh/non-github/no-token ⇒ git inherits os.environ unchanged. Only the
    # network op (clone) needs it; checkout/rev-parse are local.
    auth = _git_auth_env(url)
    clone_env = {**os.environ, **auth} if auth else None
    if ref and _SHA_RE.match(ref):
        # A specific commit: full clone (shallow can't reliably check out an
        # arbitrary SHA), then check it out.
        _git(
            "clone",
            "--no-hardlinks",
            "--no-recurse-submodules",
            url,
            str(dest),
            timeout=_CLONE_TIMEOUT_S,
            env=clone_env,
        )
        _git("checkout", ref, cwd=dest)
    elif ref:
        # A tag or branch: shallow clone of just that ref.
        _git(
            "clone",
            "--depth",
            "1",
            "--no-hardlinks",
            "--no-recurse-submodules",
            "--branch",
            ref,
            url,
            str(dest),
            timeout=_CLONE_TIMEOUT_S,
            env=clone_env,
        )
    else:
        _git(
            "clone",
            "--depth",
            "1",
            "--no-hardlinks",
            "--no-recurse-submodules",
            url,
            str(dest),
            timeout=_CLONE_TIMEOUT_S,
            env=clone_env,
        )
    return _git("rev-parse", "HEAD", cwd=dest)


# --- Git-less fetch for the frozen desktop app (ADR 0058 D1) ---------------
# The frozen PyInstaller sidecar has no `git` (and no `pip`), but the loader
# already discovers + importlib-loads plugins from the live root in frozen mode.
# So the only gap is *fetching* the code: download a GitHub archive tarball over
# HTTPS (the bundled httpx) and extract it — an on-disk result identical to a
# shallow clone. `git` stays the path on a dev/server box (history, ssh, and
# private auth — over ssh keys / credential helpers, or a github HTTPS token via
# `_git_auth_env`); the archive path is preferred when git is absent or we're frozen.

_GH_RE = re.compile(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$")


def _frozen_like() -> bool:
    """True in the frozen desktop sidecar (no git/pip). ``PROTOAGENT_PLUGIN_FROZEN``
    lets a dev box simulate it for testing."""
    return bool(getattr(sys, "frozen", False)) or os.environ.get("PROTOAGENT_PLUGIN_FROZEN") == "1"


def _prefer_archive() -> bool:
    """Use the git-less HTTPS-archive fetch instead of ``git clone``? Forced either
    way by ``PROTOAGENT_PLUGIN_FETCH=archive|git`` (testing); otherwise when we're
    frozen or git isn't on PATH."""
    mode = os.environ.get("PROTOAGENT_PLUGIN_FETCH", "").strip().lower()
    if mode == "archive":
        return True
    if mode == "git":
        return False
    return _frozen_like() or shutil.which("git") is None


def _github_owner_repo(url: str) -> tuple[str, str]:
    m = _GH_RE.search(url.strip())
    if not m:
        raise InstallError(
            f"git-less install needs a github.com URL (the desktop runtime can't run git) — got {url!r}."
        )
    return m.group(1), m.group(2)


def _http_get(url: str, *, accept: str | None = None) -> "object":
    """GET ``url`` following redirects (codeload), raising InstallError on failure.
    Sends a GitHub token from ``GITHUB_TOKEN``/``GH_TOKEN`` if set (private repos +
    higher rate limits)."""
    import httpx

    headers = {"User-Agent": "protoAgent-plugin-installer"}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
        return resp
    except httpx.HTTPError as e:
        raise InstallError(f"fetch failed for {url}: {e}") from e


def _resolve_sha_github(owner: str, repo: str, ref: str | None) -> str:
    """Resolve ``ref`` (branch/tag, or the default branch when empty) to a full
    commit SHA via the GitHub API — the git-less equivalent of ``git ls-remote``."""
    api = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref or 'HEAD'}"
    resp = _http_get(api, accept="application/vnd.github.sha")
    sha = resp.text.strip()
    if not _SHA_RE.match(sha) or len(sha) != 40:
        raise InstallError(f"could not resolve {ref or 'HEAD'} at {owner}/{repo} (got {sha[:80]!r}).")
    return sha


def _safe_extract_tar(data: bytes, dest: Path) -> None:
    """Extract a GitHub ``tar.gz`` into ``dest``, stripping the single top-level
    ``<repo>-<sha>/`` component. Path-traversal-safe (rejects abs paths / ``..``)
    and ignores symlinks/special files — a plugin repo is plain files + dirs."""
    dest = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            inner = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not inner:
                continue
            target = (dest / inner).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise InstallError(f"unsafe path in archive: {m.name!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is not None:
                    target.write_bytes(src.read())
            # symlinks/devices are skipped on purpose (a supply-chain vector)


def _fetch_archive(url: str, ref: str | None, dest: Path) -> str:
    """Git-less fetch: resolve ``ref`` → SHA, download the GitHub archive at that
    SHA, extract into ``dest``. Returns the resolved SHA (pinned in the lock)."""
    owner, repo = _github_owner_repo(url)
    sha = ref if (ref and _SHA_RE.match(ref) and len(ref) == 40) else _resolve_sha_github(owner, repo, ref)
    resp = _http_get(f"https://codeload.github.com/{owner}/{repo}/tar.gz/{sha}")
    dest.mkdir(parents=True, exist_ok=True)
    _safe_extract_tar(resp.content, dest)
    return sha


def _fetch(url: str, ref: str | None, dest: Path) -> str:
    """Fetch the plugin repo at ``ref`` into ``dest``; return the resolved SHA.
    ``git`` on a dev/server box; the git-less HTTPS archive (GitHub) when git is
    unavailable or in the frozen desktop app (ADR 0058 D1)."""
    if _prefer_archive():
        return _fetch_archive(url, ref, dest)
    return _clone(url, ref, dest)


# --- Bundled-dep gate for the frozen app (ADR 0058 D2) ---------------------
# The frozen runtime has no pip, so a plugin can only run if its declared
# `requires_pip` are ALREADY importable in the bundle. Gate at install time with
# a clear refusal rather than a cryptic enable-time ImportError.

_PKG_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _dep_pkg_name(spec: str) -> str:
    """The distribution name from a PEP 508 spec (``websockets>=12`` → ``websockets``)."""
    m = _PKG_NAME_RE.match(spec or "")
    return m.group(1) if m else ""


def _importable(pkg: str) -> bool:
    """Is ``pkg`` present in this runtime? Distribution metadata first, then a
    best-effort module-name probe (covers deps whose .dist-info isn't bundled)."""
    import importlib.metadata as md
    import importlib.util as iu

    try:
        md.version(pkg)
        return True
    except md.PackageNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — metadata read is best-effort
        pass
    try:
        return iu.find_spec(pkg.replace("-", "_")) is not None
    except (ImportError, ValueError):
        return False


def _deps_satisfied(deps: list[str], scopes: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """(all satisfied?, [missing dist names]) for a plugin's ``requires_pip``.

    A dep is satisfied when it's importable in THIS process — or, in the frozen desktop
    app, when it's installed in the managed Python runtime (ADR 0094 P2). The two have
    separate site-packages: a compute plugin's deps live in the child runtime (its
    ``execute_code`` skills import them there), never in the host, so a host-only check
    would report them forever-missing and the "Install deps" action could never clear.

    ``scopes`` (#2246) carves out the opposite error. A ``host``-scoped dep is one the
    plugin imports IN-PROCESS, so the managed runtime cannot satisfy it — and accepting
    the runtime's copy made the gate answer "satisfied" about the wrong interpreter:
    install passed, then every tool call died with ``ModuleNotFoundError``. Host-scoped
    deps are therefore judged by host importability alone. Unscoped deps default to
    ``runtime`` (the compute-plugin pattern), so existing manifests are unaffected.

    Known blind spot, deliberately left in place: an unscoped dep is checked against the
    HOST first, and anything core or the doc stack bundles into the frozen app (pypdf,
    python-docx, lxml, httpx, …) is importable there — so it reads "satisfied" even when
    the plugin only imports it inside ``execute_code``, where the host's PYZ copy is
    unreachable. Judging unscoped deps by the runtime alone would refuse every plugin
    whose tools import a bundled lib in-process until the runtime is provisioned, and
    ``scope: runtime`` isn't recorded (only ``host`` is), so it can't be told apart. What
    covers the document skills instead is the managed runtime's baseline
    (``apps/desktop/sidecar/requirements-docs.txt``) listing every library they import —
    pypdf was missing from it, which is how "read a PDF via execute_code" broke on desktop."""
    runtime_dists = _managed_runtime_dists()
    scopes = scopes or {}
    missing = []
    for spec in deps:
        name = _dep_pkg_name(spec)
        if not name:
            continue
        if _importable(name):
            continue
        # A host-scoped dep can only be satisfied by THIS interpreter, never the child.
        if scopes.get(name) == "host" or _normalize_dist(name) not in runtime_dists:
            missing.append(name)
    return (not missing, missing)


def _managed_runtime_dists() -> set[str]:
    """Normalized dist names in the managed runtime — only consulted in the frozen app
    (a source run's ``sys.executable`` IS the host, so host-importability already
    covers it). Best-effort: never let a runtime read break dep resolution."""
    if not _frozen_like():
        return set()
    try:
        from infra.python_runtime import managed_runtime_distributions

        return managed_runtime_distributions()
    except Exception:  # noqa: BLE001 — runtime discovery must never break enable/install
        log.debug("[plugins] managed runtime read failed (ignored)", exc_info=True)
        return set()


def _normalize_dist(name: str) -> str:
    from infra.python_runtime import normalize_dist as _norm

    return _norm(name)


def _frozen_install_missing_deps(
    pid: str, requires_pip: list[str], missing: list[str], scopes: dict[str, str] | None = None
) -> None:
    """Frozen desktop, hard deps missing at install/update time: pip them into the
    managed Python runtime (ADR 0094 P2) — the same target ``install_deps`` uses —
    instead of the pre-ADR-0093 flat refusal (#2226). Refuses only when the runtime
    isn't provisioned (naming the install route) or the install itself fails
    (surfacing pip's real error).

    A ``host``-scoped dep (#2246) is refused up front and NOT sent to the runtime:
    installing it there would "succeed" while leaving the in-process import that
    actually needs it just as broken, which is the whole failure this scope exists to
    stop. On a frozen host that dep is genuinely unsatisfiable, so say so — and say why
    — rather than passing the gate and crashing at tool time."""
    # Module (not from-) imports: callables resolve through the source module at call
    # time, so test monkeypatches on infra.python_runtime / runtime.python_install bind.
    import infra.python_runtime as pr
    import runtime.python_install as pi

    host_scoped = [n for n in missing if (scopes or {}).get(n) == "host"]
    if host_scoped:
        raise InstallError(
            f"{pid!r} needs {', '.join(host_scoped)} as a HOST-scoped dep, which a frozen app "
            f"cannot satisfy: the plugin imports it in this process, and the managed Python "
            f"runtime (which is where deps get installed) only serves execute_code children — "
            f"separate site-packages. Vendor the code, drop the dependency, or ship it in the "
            f"app bundle. (Declare scope: runtime if it is only imported by execute_code.)"
        )

    to_install = [s for s in requires_pip if _dep_pkg_name(s) in missing]
    _validate_pip_specs(pid, to_install)
    if pr.managed_python_exe() is None:
        raise InstallError(
            f"{pid!r} needs {', '.join(missing)} which isn't in the desktop runtime — "
            f"provision the managed Python runtime first (POST /api/runtime/python/install "
            f"or Settings ▸ Tools), then retry."
        )
    try:
        pi.install_requirements_into_managed_runtime(to_install)
    except pi.PythonRuntimeError as exc:
        _audit(
            "install_deps",
            {"id": pid, "deps": to_install, "targets": ["managed-runtime"]},
            "managed runtime install failed",
            success=False,
        )
        raise InstallError(
            f"{pid!r} needs {', '.join(missing)} — installing into the managed runtime failed: {exc}"
        ) from exc
    _audit("install_deps", {"id": pid, "deps": to_install, "targets": ["managed-runtime"]}, "ok")
    log.info("[plugins] %s: installed %d missing dep(s) into the managed runtime", pid, len(to_install))


def install(
    url: str, ref: str | None = None, *, force: bool = False, by: str = "cli", allow: list[str] | None = None
) -> dict:
    """Clone a plugin from ``url`` (at ``ref``) into the live plugins dir, pinned
    to its resolved SHA, and record it in ``plugins.lock``. Does NOT enable it or
    install its deps. Returns the install summary.

    A ``url`` a bundled plugin ``supersedes`` fetches nothing: that plugin ships with
    protoAgent now, so the summary describes the bundled copy and carries
    ``superseded: True`` (``--force`` doesn't change that — override a bundled plugin
    from a fork URL instead). Checked before the allowlist and the fetch: no code comes
    from that source, and the retired repo may no longer be reachable."""
    _validate_url(url)
    if ref:
        _validate_ref(ref)  # before it reaches git or the GitHub API URL (PR #1140 QA)
    bundled = superseding_plugin(url)
    if bundled is not None:
        log.info(
            "[plugins] %s ships with protoAgent (bundled v%s, supersedes %s) — nothing to fetch",
            bundled.id,
            bundled.version,
            url,
        )
        summary = _summary(bundled, source=url, ref=ref or "", sha="")
        summary["superseded"] = True
        return summary
    if not _source_allowed(url, allow):
        detail = (
            "plugins.sources.allow is an explicit empty list (deny-all) — list the origins you trust, "
            "or remove the key to allow any source."
            if allow is not None and not allow
            else f"source {url!r} is not on plugins.sources.allow — add it or install from an allowed origin."
        )
        raise InstallError(detail)

    target_root = live_plugins_dir()
    target_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pa-plugin-") as tmp:
        staging = Path(tmp) / "repo"
        sha = _fetch(url, ref, staging)

        # A bundle repo (protoagent.bundle.yaml) carries no code — it names a set of
        # plugin repos to install together. Fan out to per-plugin install().
        bundle = load_bundle(staging)
        if bundle is not None:
            return _install_bundle(bundle, url, sha, ref, force=force, by=by, allow=allow)

        manifest = load_manifest(staging)
        if manifest is None:
            raise InstallError(
                f"{url!r} has no protoagent.plugin.yaml or protoagent.bundle.yaml — not a protoAgent plugin or bundle."
            )
        pid = manifest.id

        # No silent shadowing of a built-in (repo) plugin. A manifest-less ghost
        # dir (e.g. a __pycache__ leftover) is not a built-in and must not block.
        if _is_builtin(pid):
            raise InstallError(f"plugin id {pid!r} is a built-in — cannot install over it.")

        # Frozen runtime (desktop): no host pip — a missing hard dep used to be a flat
        # ADR 0058 D2 refusal ("install it on a server instead"). The managed Python
        # runtime (ADR 0094 P2) is a real install target now, so when it's provisioned
        # the missing deps are pip'd into it right here (#2226), and we refuse only when
        # the runtime is absent or that install actually fails.
        # OPTIONAL deps (#1953) don't gate: the plugin degrades gracefully without
        # them, so a missing one warns (in the summary + log) and install proceeds.
        warnings: list[str] = []
        if _frozen_like():
            if manifest.requires_pip:
                ok, missing = _deps_satisfied(manifest.requires_pip, manifest.pip_scopes)
                if not ok:
                    _frozen_install_missing_deps(pid, manifest.requires_pip, missing, manifest.pip_scopes)
            if manifest.optional_pip:
                _, soft_missing = _deps_satisfied(manifest.optional_pip, manifest.pip_scopes)
                if soft_missing:
                    warn = (
                        f"optional dep(s) {', '.join(soft_missing)} aren't in the desktop runtime — "
                        f"installed anyway; the features of {pid!r} that need them degrade until they're available."
                    )
                    warnings.append(warn)
                    log.warning("[plugins] %s: %s", pid, warn)

        target = target_root / pid
        if target.exists():
            # Re-install from the plugin's OWN recorded origin is a CONVERGE, not a
            # conflict: same commit → no-op, moved pin/ref → update — so re-running a
            # bundle install never dies on its own already-installed members. --force
            # stays the lever for the real conflicts: an id-colliding install from a
            # DIFFERENT source, or a dir the lock doesn't know (a working-tree /
            # hand-copied plugin that a git install must not silently clobber).
            prior = _lock_entry(pid)
            same_source = prior is not None and prior.get("source_url") == url
            if not force and not same_source:
                origin = f"from {prior.get('source_url')!r}" if prior else "untracked (no plugins.lock entry)"
                raise InstallError(f"plugin {pid!r} already installed — {origin}; use --force to replace.")
            if not force and same_source and prior.get("resolved_sha") == sha:
                log.info("[plugins] %s already at %s from %s — up to date", pid, sha[:10], url)
                summary = _summary(manifest, source=url, ref=ref or "", sha=sha)
                summary["up_to_date"] = True
                if warnings:
                    summary["warnings"] = warnings
                return summary

        shutil.rmtree(staging / ".git", ignore_errors=True)  # drop git metadata; lock holds provenance

        # Land the staged tree with a swap, not rmtree-then-move (#3075): the old sequence
        # deleted the installed copy first, so a move that died mid-copy (disk full,
        # permissions) left NO old version and half a new one. Rename the existing install
        # aside instead — same parent dir, so it's an atomic same-filesystem rename — move
        # the staged tree in, and only then drop the backup; any failure renames the old
        # version back. (`plugins.lock` already lands atomically via `_write_lock`.)
        backup = target.parent / (target.name + ".bak")
        _discard(backup)  # leftover from a previously interrupted swap
        backed_up = False
        if target.exists() or _is_link(target):
            try:
                os.rename(target, backup)
                backed_up = True
            except OSError as exc:
                raise InstallError(
                    f"could not set aside the installed copy of {pid!r} before update "
                    f"(it was left untouched): {exc}"
                ) from exc
        try:
            shutil.move(str(staging), str(target))
        except Exception as exc:
            # A cross-filesystem move copies then deletes — it can fail half-copied.
            shutil.rmtree(target, ignore_errors=True)
            restored = ""
            if backed_up:
                try:
                    os.rename(backup, target)
                    restored = " — the previous version was restored"
                except OSError:
                    restored = f" — the previous version was left at {backup}"
            raise InstallError(f"could not move staged plugin {pid!r} into place{restored}: {exc}") from exc
        if backed_up:
            _discard(backup)

        manifest = load_manifest(target) or manifest  # re-read from final path

    summary = _summary(manifest, source=url, ref=ref or "", sha=sha)
    if warnings:
        summary["warnings"] = warnings
    lock = _read_lock()
    prior_entry = _lock_rows_by_id(lock).get(pid)
    entry = {
        "id": pid,
        "source_url": url,
        "requested_ref": ref or "",
        "resolved_sha": sha,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "by": by,
    }
    if prior_entry is not None and isinstance(prior_entry.get("deps"), list):
        entry["deps"] = prior_entry["deps"]
    lock["plugins"] = [e for e in lock["plugins"] if e.get("id") != pid]
    lock["plugins"].append(entry)
    _write_lock(lock)
    _audit("install", {"url": url, "ref": ref or "", "sha": sha, "id": pid}, f"installed {pid}@{sha[:10]}")
    log.info("[plugins] installed %s@%s from %s", pid, sha[:10], url)
    return summary


BUNDLE_FILENAME = "protoagent.bundle.yaml"


def load_bundle(repo: Path) -> dict | None:
    """Parse ``<repo>/protoagent.bundle.yaml`` → a bundle dict, or ``None`` if it's
    absent/invalid. A **bundle** is a reference manifest: it names a set of plugin
    repos (``{id, url, ref}`` or ``{id, builtin: true}``) to install together, plus a
    suggested ``enabled`` list + ``config``. It carries no plugin code of its own."""
    import yaml

    f = repo / BUNDLE_FILENAME
    if not f.exists():
        return None
    try:
        doc = yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or not doc.get("id") or not isinstance(doc.get("plugins"), list):
        return None
    return doc


# The input types a bundle's `config_inputs:` may declare (#2934). Mirrors the MCP
# catalog input shape ({key, label, type, required?, default?}); the SetupWizard /
# NewAgentPanel Configure step picks the widget from `type` (string/path → text,
# delegate → dropdown of configured ACP delegates, boolean → toggle).
CONFIG_INPUT_TYPES = ("string", "path", "delegate", "boolean")

# Core sections a bundle may never prompt for through `config_inputs:` — the Configure
# step writes answers (and manifest `default`s, with no operator involvement) straight
# into the tracked config, and `required: true` is a hard gate ("type it to proceed").
# A bundle is trusted code, but a Configure prompt labelled "API key" that lands in
# `model.api_key`, or one that silently widens `network`/`egress`/`projects`, is a lying
# form. Plugin sections (`project_board.*`, `github.*`) are what the mechanism is for.
CONFIG_INPUT_RESERVED_SECTIONS = frozenset(
    {
        "model", "auth", "network", "security", "egress", "plugins", "filesystem",
        "projects", "onboarding", "delegates", "identity", "instance", "secrets", "mcp",
        "self_improvement",
    }
)

# A declared `key` is a DOTTED CONFIG PATH ("section.key[...]") the install path writes
# the operator's answer to — at least two safe segments, so a bundle can't declare a
# bare top-level key (which would replace a whole section) or smuggle YAML weirdness.
_CONFIG_INPUT_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)+$")


def normalize_config_inputs(bundle_id: str, raw: object, *, strict: bool = True) -> list[dict]:
    """Validate + normalize a bundle's ``config_inputs:`` block (#2934) into
    ``[{key, label, type, required, default?}]``. ``strict`` (the install path) raises
    :class:`InstallError` on a malformed entry so a typo'd manifest fails the install
    with a reason instead of silently dropping the operator prompt; ``strict=False``
    (the read-only peek) drops bad entries so one typo can't blank the whole preview."""

    def bad(reason: str) -> None:
        if strict:
            raise InstallError(f"bundle {bundle_id!r}: invalid config_inputs entry — {reason}")

    if raw is None:
        return []
    if not isinstance(raw, list):
        bad("config_inputs must be a list")
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            bad(f"{entry!r} is not a mapping")
            continue
        key = str(entry.get("key", "")).strip()
        if not _CONFIG_INPUT_KEY_RE.fullmatch(key):
            bad(f"key {entry.get('key')!r} is not a dotted config path (e.g. section.key)")
            continue
        if key.split(".", 1)[0] in CONFIG_INPUT_RESERVED_SECTIONS:
            bad(f"key {key!r} targets a core section a bundle may not prompt for")
            continue
        label = str(entry.get("label", "")).strip()
        if not label:
            bad(f"{key!r} has no label")
            continue
        typ = str(entry.get("type") or "string").strip().lower()
        if typ not in CONFIG_INPUT_TYPES:
            bad(f"{key!r} has unknown type {entry.get('type')!r} (known: {', '.join(CONFIG_INPUT_TYPES)})")
            continue
        norm: dict = {"key": key, "label": label, "type": typ, "required": bool(entry.get("required"))}
        # `project: true` on a `path` input (#PM-first-run): the answered path is a repo
        # the agent MANAGES — the create path also registers it in the ADR 0095
        # `projects:` registry (fs fence, GitHub picker, board), records its GitHub
        # remote, and scopes `onboarding.root` to its parent. A plain flag, ignored by
        # hosts that predate it, so a bundle can declare it without a core-version gate.
        if typ == "path" and bool(entry.get("project")):
            norm["project"] = True
        if "default" in entry and entry["default"] is not None:
            default = coerce_config_input_value(typ, entry["default"])
            if default is not None:
                norm["default"] = default
        out.append(norm)
    return out


def coerce_config_input_value(typ: str, value: object) -> object | None:
    """Coerce an operator-supplied (or manifest-default) config_inputs value to its
    declared ``type``. Returns the value to write into the config YAML, or ``None``
    when it can't be interpreted (blank text, an unrecognized boolean word) — the
    caller skips the write rather than persisting junk. A quoted ``"false"`` must
    come out ``False``, so booleans parse words, never truthiness."""
    if typ == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        return None
    text = str(value).strip()
    return text or None


def bundle_config_overlay(bundle_config: dict | None, current: dict | None) -> dict:
    """Reduce a bundle's recommended ``config:`` ({section: {key: val}}) to a DEFAULTS
    overlay: only the keys the operator hasn't already set in ``current`` (the live
    config sections). Per-section, per-leaf — an operator value always wins and a
    present key is left untouched, so applying this never clobbers existing settings.
    Empty sections are dropped. Shared by the console install route and the fleet
    create path so both apply bundle defaults identically (#1350)."""
    overlay: dict = {}
    cur = current if isinstance(current, dict) else {}
    for section, values in (bundle_config or {}).items():
        if not isinstance(values, dict):
            continue
        existing = cur.get(section)
        existing = existing if isinstance(existing, dict) else {}
        fill = {k: v for k, v in values.items() if k not in existing}
        if fill:
            overlay[str(section)] = fill
    return overlay


# Every archetype: key the reader (`fleet_routes._archetypes`) consumes. The block is
# otherwise cached verbatim into plugins.lock, so anything outside this set is dead
# weight the author almost certainly misspelled.
_ARCHETYPE_KEYS = {"label", "icon", "blurb", "soul", "soul_preset", "tier", "requires", "requires_tools"}


def _checked_archetype_block(bundle_id: str, arch: dict | None) -> dict:
    """The bundle's ``archetype:`` block, warning on keys no reader consumes (#2715)."""
    arch = dict(arch or {})
    unknown = sorted(set(arch) - _ARCHETYPE_KEYS)
    if unknown:
        log.warning(
            "[plugins] bundle %s archetype: block has unknown key(s) %s — known keys: %s",
            bundle_id,
            ", ".join(unknown),
            ", ".join(sorted(_ARCHETYPE_KEYS)),
        )
    return arch


def _install_bundle(
    bundle: dict, bundle_url: str, bundle_sha: str, ref: str | None, *, force: bool, by: str, allow: list[str] | None
) -> dict:
    """Install every plugin a bundle names (reusing single-plugin ``install()`` for
    each — so each member is allow-checked + pinned in ``plugins.lock`` exactly as a
    direct install), then record the bundle for provenance. Enable + config are
    *suggested* in the return value, never applied (install ≠ enable ≠ trust)."""
    bid = str(bundle.get("id"))
    # Validate the declared operator prompts (#2934) BEFORE fetching any member — a
    # malformed config_inputs entry fails the install with a reason, not after N clones.
    config_inputs = normalize_config_inputs(bid, bundle.get("config_inputs"))
    installed: list[dict] = []
    skipped: list[str] = []
    superseded: list[str] = []
    bundle_warnings: list[str] = []
    for entry in bundle.get("plugins") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("builtin"):
            skipped.append(str(entry.get("id", "?")))  # ships with protoAgent — nothing to fetch
            continue
        purl = entry.get("url")
        if not purl:
            raise InstallError(f"bundle {bid!r}: plugin {entry.get('id', '?')!r} has no url")
        # A member listed by a URL a bundled plugin supersedes moved into core: treat it
        # like `builtin: true` (skip, don't fetch), so an archetype repo that still lists
        # the old URL installs unchanged on hosts old and new. Bundles carry no
        # min-version, so the repo can't be re-pointed at `builtin: true` without
        # breaking every host that predates the move.
        moved = superseding_plugin(str(purl))
        if moved is not None:
            from graph.plugins.manifest import display_source

            log.info(
                "[plugins] bundle %s: member %s ships with protoAgent now (bundled v%s supersedes %s) — skipped",
                bid,
                entry.get("id", moved.id),
                moved.version,
                display_source(str(purl)),
            )
            superseded.append(moved.id)
            # The member's pin is a floor (#2960). A pin AHEAD of what protoAgent ships
            # can't be honoured — the bundled copy is the member now — so say so instead
            # of silently running an older version than the archetype asked for.
            pin, shipped = _semver_key(str(entry.get("ref") or "")), _semver_key(moved.version)
            if pin is not None and shipped is not None and pin > shipped:
                warn = (
                    f"{moved.id}: the bundle pins {entry.get('ref')}, but this protoAgent ships "
                    f"{moved.id} v{moved.version} (it supersedes the git repo) — running the bundled "
                    f"copy; update protoAgent for the newer version."
                )
                bundle_warnings.append(warn)
                log.warning("[plugins] bundle %s: %s", bid, warn)
            continue

        member_ref = entry.get("ref")
        # Independent member semver chase (#2960): a release-tag pin in the bundle
        # manifest is a FLOOR, not the answer — an operator may have force-installed
        # the member ahead of the archetype's pin, and blindly re-pinning to the
        # manifest would DOWNGRADE it. ls-remote the member repo (via
        # check_plugin_update, the same newest-semver-tag logic the single-plugin
        # update route rides) and install the newest tag when one exists. SHA pins
        # and branch refs pass through untouched.
        #
        # BOUNDED by caret semantics (ADR 0049 amendment): the chase was unbounded, so a
        # member's first breaking release propagated to every archetype spawn automatically,
        # over a pin that looked like it was preventing exactly that. The floor now means
        # "this version or a compatible newer one", which is what a pin was always read as.
        if member_ref and is_release_tag(member_ref):
            try:
                member_lock = {
                    "id": str(entry.get("id", "")),
                    "source_url": str(purl),
                    "requested_ref": member_ref,
                    "resolved_sha": "",
                }
                status = check_plugin_update(member_lock)
                latest = status.get("latest_ref")
                if latest and is_compatible_upgrade(member_ref, latest):
                    member_ref = latest
                elif latest:
                    log.info(
                        "[plugins] bundle %s: member %s stays at %s — %s is outside the "
                        "compatible range; bump the manifest pin deliberately to adopt it",
                        bid, entry.get("id", "?"), member_ref, latest,
                    )
            except Exception:  # noqa: BLE001 — best-effort; fall back to the manifest pin
                pass

        installed.append(install(str(purl), member_ref, force=force, by=f"bundle:{bid}", allow=allow))

    lock = _read_lock()
    lock.setdefault("bundles", [])
    lock["bundles"] = [b for b in lock["bundles"] if b.get("id") != bid]
    lock["bundles"].append(
        {
            "id": bid,
            # Display name persisted so the console's Installed table can label member
            # rows with their bundle without re-fetching the manifest (older locks lack
            # it — consumers fall back to the id).
            "name": str(bundle.get("name") or ""),
            "source_url": bundle_url,
            "requested_ref": ref or "",
            "resolved_sha": bundle_sha,
            "plugins": [s["id"] for s in installed],
            # Members this bundle names by a URL a bundled plugin supersedes — not fetched
            # (the bundled copy is the member now), so not in `plugins`, which stays "the
            # code this bundle installed" for ownership/uninstall. Kept so the no-`enabled`
            # fallback ("turn on every member") still turns them on, as it did before the
            # plugin moved into core.
            "superseded": superseded,
            # The bundle's curated turn-on list (a subset of `plugins`). Cached here so a
            # consumer that only sees the lock — e.g. the fleet new-agent path, which
            # installs via a CLI subprocess and never sees the live install summary — can
            # auto-enable exactly what the bundle author intended. Empty = enable all members.
            "enabled": list(bundle.get("enabled") or []),
            # The bundle's recommended per-plugin config defaults ({section: {key: val}}).
            # Cached for the same lock-only consumer; applied as DEFAULTS (operator values
            # win, present keys are never clobbered — see `bundle_config_overlay`).
            "config": dict(bundle.get("config") or {}),
            # The bundle's MCP servers to seed (ADR 0083 D5, #2011): catalog-shaped
            # {template, inputs} items. Cached for the lock-only create path, which seeds
            # them into the workspace's `mcp.servers` via `_apply_bundle_mcp_servers` —
            # `config:`'s dict-leaf overlay can't merge the `mcp.servers` list.
            "mcp": list(bundle.get("mcp") or []),
            # The bundle's declared secrets ({key, label, placeholder, secret, required} —
            # same shape as an mcp-catalog input). Cached alongside `mcp` so the lock-only
            # create path can prompt for / seed these inputs without re-parsing the bundle
            # manifest (#2041, slice 1).
            "secrets": list(bundle.get("secrets") or []),
            # The plugin config keys the SetupWizard/NewAgentPanel Configure step prompts
            # for at create time (#2934) — {key: dotted config path, label, type, required,
            # default?}, normalized above. Cached so the lock-only seed path
            # (`apply_bundle_config_inputs`) can anchor operator values to DECLARED keys.
            "config_inputs": config_inputs,
            # Archetype metadata (ADR 0042) cached here so the new-agent picker can offer
            # this bundle as a starter type without re-reading its manifest. Unknown keys
            # are cached but never read — warn (not fail) so a typo'd field (`souls:`,
            # `require_tools:`) surfaces at install instead of vanishing silently (#2715).
            "archetype": _checked_archetype_block(bid, bundle.get("archetype")),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "by": by,
        }
    )
    _write_lock(lock)
    _audit(
        "install-bundle",
        {"url": bundle_url, "sha": bundle_sha, "id": bid},
        f"installed bundle {bid} ({len(installed)} plugin(s))",
    )
    log.info("[plugins] installed bundle %s@%s (%d plugins) from %s", bid, bundle_sha[:10], len(installed), bundle_url)
    summary = {
        "bundle": bid,
        "name": bundle.get("name", ""),
        "description": bundle.get("description", ""),
        "resolved_sha": bundle_sha,
        "installed": installed,
        "skipped_builtin": skipped,
        # Members listed by a URL a bundled plugin supersedes — skipped like builtins.
        "skipped_superseded": superseded,
        "enabled": list(bundle.get("enabled") or []),
        "config": bundle.get("config") or {},
        "config_inputs": config_inputs,
    }
    if bundle_warnings:
        summary["warnings"] = bundle_warnings
    return summary


def _clean_config_refs(plugin_id: str, section: str, purge: bool) -> bool:
    """Remove the plugin's references from the live langgraph-config.yaml (ADR 0027):
    always the `plugins.enabled`/`disabled` entry (a dangling enabled entry is just
    broken); with ``purge`` also the plugin's `config_section` block. Comment-safe
    (ruamel). Returns True if anything changed."""
    from graph.config_io import CONFIG_WRITE_LOCK, config_yaml_path, load_yaml_doc, save_yaml_doc

    cfg = config_yaml_path()
    if not cfg.exists():
        return False
    # Across the read AND the write (#2743): the server's applier rewrites this file
    # under the same lock, so a scrub interleaved with it either resurrects the
    # uninstalled id or drops an enable that landed in between.
    with CONFIG_WRITE_LOCK:
        doc = load_yaml_doc(cfg)
        if not isinstance(doc, dict):
            return False
        changed = False
        plugins = doc.get("plugins")
        if isinstance(plugins, dict):
            for key in ("enabled", "disabled"):
                lst = plugins.get(key)
                if isinstance(lst, list) and plugin_id in lst:
                    while plugin_id in lst:
                        lst.remove(plugin_id)
                    changed = True
        if purge and section in doc:
            del doc[section]
            changed = True
        if changed:
            save_yaml_doc(doc, cfg)
    return changed


def _clean_secrets(section: str) -> bool:
    """Remove the plugin's section from the live secrets.yaml overlay (purge only)."""
    from graph.config_io import CONFIG_WRITE_LOCK, load_yaml_doc, save_yaml_doc, secrets_yaml_path

    sec = secrets_yaml_path()
    if not sec.exists():
        return False
    # Same lock as the applier's `save_secrets` read-modify-write (#2743): interleaved,
    # one drops the other's secret update or brings the purged section back.
    with CONFIG_WRITE_LOCK:
        doc = load_yaml_doc(sec)
        if isinstance(doc, dict) and section in doc:
            del doc[section]
            save_yaml_doc(doc, sec)
            return True
    return False


def uninstall(plugin_id: str, *, purge: bool = False) -> dict:
    """Remove a git-installed plugin and its references. ALWAYS removes the code
    dir, the `plugins.lock` entry, and the `plugins.enabled`/`disabled` reference.
    With ``purge=True`` ALSO removes the plugin's config section + its secrets.
    Built-ins are refused; pip deps are NEVER auto-removed (shared venv) — they're
    returned for the operator to remove. Returns a report dict.

    A built-in id is accepted only when there is an installed copy of it to remove — one
    the loader is ignoring in favour of the bundled copy (see ``_uninstall_superseded``):
    either recorded from a URL the bundled manifest ``supersedes``, or UNTRACKED (no lock
    row at all — a folder dropped in or symlinked by hand). Both used to be refused with
    "is a built-in"; for the untracked one that was a regression the moment a plugin moved
    into core, since removing it worked right up until then. A copy recorded from some
    OTHER url — a deliberate fork override — is still refused: it is the running copy and
    a recorded choice, not a leftover.

    What gets deleted is never "whatever sits at <live>/<id>" — only the path the loader
    itself found as that plugin's copy (``_removable_copy``). A different plugin's folder,
    a folder with no plugin in it, and the bundled tree can never be that path. The
    ordinary (non-bundled) uninstall applies the same rule (``_plain_copy_refusal``): since
    #3452 ``<live>`` is ``plugins.dir`` when set, often a folder of checkouts. A dangling
    link at exactly ``<live>/<id>`` is unlinked (it has no target to harm) and named in
    the report as ``dangling_link``."""
    if _is_builtin(plugin_id):
        bundled = _bundled_manifest(plugin_id)
        recorded = recorded_source_url(plugin_id)
        # A copy RECORDED from a URL the bundled copy doesn't supersede is a deliberate fork
        # override — refused, unchanged. (No bundled manifest at all: a bundle dir.)
        if bundled is None or (recorded and bundled_superseding(plugin_id, recorded) is None):
            raise InstallError(f"{plugin_id!r} is a built-in plugin — not removable via uninstall.")
        target, why_not = _removable_copy(plugin_id)
        if target is None and not recorded:
            # Untracked, and nothing the loader vouches for: refuse, delete nothing.
            raise InstallError(
                why_not
                or f"{plugin_id!r} ships with protoAgent and there is no installed copy of it in "
                f"{live_plugins_dir()} — nothing to uninstall. To turn the plugin off, disable it instead."
            )
        # A tracked superseded row still gets its lock entry cleared — which removes no files.
        # If something IS on disk that the guards refused, that reason rides along in the
        # report (`left_in_place`) instead of vanishing behind a "removed: lock" success.
        return _uninstall_superseded(plugin_id, bundled, purge=purge, target=target, left_in_place=why_not)
    live_root = live_plugins_dir()
    target = live_root / plugin_id
    dangling = _is_link(target) and not target.exists()
    # Read the manifest BEFORE deleting — purge needs the config section + we report
    # the declared deps.
    manifest = None if dangling else (load_manifest(target) if (target / MANIFEST_FILENAME).exists() else None)
    refusal = _plain_copy_refusal(plugin_id, target, live_root, manifest, dangling=dangling)
    if refusal:
        raise InstallError(refusal)
    section = (manifest.config_section if manifest else "") or plugin_id
    deps_left = [*manifest.requires_pip, *manifest.optional_pip] if manifest else []

    removed: list[str] = []
    if target.exists() or _is_link(target):
        # Rename aside first (atomic, same parent dir), then delete (#3075): an
        # interrupted removal leaves `<id>.bak` — never a half-deleted live plugin
        # dir. A leftover backup is cleared by the next install of the same id.
        _remove_installed_copy(target)
        removed.append("code")
    lock = _read_lock()
    before = len(lock["plugins"])
    lock["plugins"] = [e for e in lock["plugins"] if not (isinstance(e, dict) and e.get("id") == plugin_id)]
    if len(lock["plugins"]) != before:
        _write_lock(lock)
        removed.append("lock")
    if _clean_config_refs(plugin_id, section, purge):
        removed.append("config" if purge else "enabled-ref")
    if purge and _clean_secrets(section):
        removed.append("secrets")

    if not removed:
        raise InstallError(f"plugin {plugin_id!r} is not installed.")

    # Plugin-owned scheduler jobs (#1642): cancel `plugin:<id>:*` so an uninstalled
    # plugin's recurring cadence can't keep firing prompts about a plugin that's gone.
    # The loader's disable-sweep can't cover this case — the manifest is gone from
    # disk. Best-effort: in a CLI process there's no live scheduler (STATE.scheduler
    # is None → 0); the console uninstall route runs in the live server where it works.
    try:
        from graph.sdk import cancel_plugin_jobs

        jobs_cancelled = cancel_plugin_jobs(plugin_id)
        from graph.plugins import setup_gaps as _setup_gaps

        _setup_gaps.clear_plugin(plugin_id)  # its setup-gap banner must not outlive it
    except Exception:  # noqa: BLE001 — job hygiene must never fail the uninstall
        jobs_cancelled = 0
    if jobs_cancelled:
        removed.append("jobs")

    _audit("uninstall", {"id": plugin_id, "purge": purge}, f"uninstalled {plugin_id} ({', '.join(removed)})")
    log.info("[plugins] uninstalled %s (%s)", plugin_id, ", ".join(removed))
    report = {
        "id": plugin_id,
        "removed": removed,
        "deps_left": deps_left,
        "purged": purge,
        "jobs_cancelled": jobs_cancelled,
    }
    if dangling:
        report["dangling_link"] = str(target)
    return report


def _uninstall_superseded(
    plugin_id: str, bundled: PluginManifest, *, purge: bool, target: Path | None, left_in_place: str = ""
) -> dict:
    """Remove the IGNORED installed copy of a plugin that now ships with protoAgent —
    whether the bundled manifest superseded its source or #1574 demoted it (an untracked
    copy, which has no lock row to remove and is often a symlinked dev checkout).

    Only that copy's files and its ``plugins.lock`` entry go. Everything keyed by the
    plugin id belongs to the bundled copy that is actually running, so it all stays —
    even under ``purge``: the ``plugins.enabled``/``disabled`` entry (dropping it would
    switch the bundled plugin off), the config section and secrets, its scheduler jobs,
    and its own setup gaps (only the loader's "superseded" banner is cleared). Declared
    deps aren't reported for removal either — the bundled copy may import the same ones.

    ``superseded_by_bundled`` in the report tells callers the bundled copy keeps running.
    ``was_loaded`` says whether THIS process had imported the copy just removed — true
    only when protoAgent was upgraded under a running server (it loaded the git copy at
    boot, before a bundled copy superseded it). Then the running code has lost its files
    and callers must unload it like any other removal (purge + reload; a mounted router
    needs a restart); otherwise there is nothing to unload."""
    from graph.plugins.manifest import display_source

    source_url = recorded_source_url(plugin_id)
    # `target` is the ONE path `_removable_copy` verified — or None: nothing on disk to
    # remove (a tracked row whose files are already gone). Nothing else is touched.
    was_loaded = _running_copy_is(plugin_id, target) if target is not None else False
    dangling = target is not None and _is_link(target) and not target.exists()
    removed: list[str] = []
    if target is not None:
        # Same rename-aside-then-delete as a normal uninstall (#3075); a symlinked copy
        # (the #2298 live-checkout workflow) is unlinked, never followed.
        _remove_installed_copy(target)
        removed.append("code")
    lock = _read_lock()
    before = len(lock["plugins"])
    lock["plugins"] = [e for e in lock["plugins"] if not (isinstance(e, dict) and e.get("id") == plugin_id)]
    if len(lock["plugins"]) != before:
        _write_lock(lock)
        removed.append("lock")
    try:
        from graph.plugins import setup_gaps as _setup_gaps
        from graph.plugins.loader import SUPERSEDED_GAP_KEY

        _setup_gaps.report(plugin_id, SUPERSEDED_GAP_KEY, None)
    except Exception:  # noqa: BLE001 — banner hygiene must never fail the uninstall
        pass
    _audit(
        "uninstall",
        {
            "id": plugin_id,
            "purge": purge,
            "superseded_by_bundled": bundled.version,
            "source_url": display_source(source_url),
        },
        f"removed the superseded copy of {plugin_id} ({', '.join(removed)}); bundled v{bundled.version} kept",
    )
    log.info(
        "[plugins] removed the superseded copy of %s from %s (%s) — the bundled v%s keeps running%s",
        plugin_id,
        display_source(source_url),
        ", ".join(removed),
        bundled.version,
        " (this process was still running the removed copy — unload it)" if was_loaded else "",
    )
    report = {
        "id": plugin_id,
        "removed": removed,
        "deps_left": [],
        "purged": False,
        "jobs_cancelled": 0,
        "superseded_by_bundled": bundled.version,
        "was_loaded": was_loaded,
    }
    if dangling:
        report["dangling_link"] = str(target)
    if target is None and left_in_place:
        report["left_in_place"] = left_in_place
        log.warning("[plugins] %s: lock entry cleared, but %s", plugin_id, left_in_place)
    return report


def _removable_copy(plugin_id: str) -> tuple[Path | None, str]:
    """The ONE path uninstall may delete for a built-in id — ``(path, "")`` — or
    ``(None, why)`` with a reason the operator can act on.

    Never "whatever sits at <live>/<id>": that folder can hold a DIFFERENT plugin (a
    renamed fork running alongside the bundled one), the operator's own work with no
    manifest at all, or — with ``plugins.dir`` aimed at the app's tree — the bundled
    plugin itself; deleting it wholesale did all three. The candidate is only ever a copy
    the LOADER itself found for this id in the live root: the one it ignored (the very
    path its banner names, whatever the folder is called) or, for an untracked copy that
    isn't older, the one it runs. Even then every guard must hold: the live root is not
    the bundled tree, the path sits directly in the live root, and it holds a manifest
    whose id is ``plugin_id``. The one exception is a DANGLING link at exactly
    ``<live>/<id>``: nothing is there to verify, and unlinking it harms nothing.
    ``(None, "")`` means there is nothing on disk at all."""
    from graph.plugins.loader import discover_plugins

    bundled_root, live_root = loader_roots()
    if _same_dir(bundled_root, live_root):
        return None, (
            f"{plugin_id!r}: the live plugins dir is protoAgent's own bundled plugins tree, so nothing "
            "in it is an installed copy — uninstall won't delete from it. Point plugins.dir somewhere else."
        )
    notes: dict = {}
    running = {m.id: m for m in discover_plugins([bundled_root, live_root], superseded=notes)}
    note = notes.get(plugin_id)
    if note:
        candidate: Path | None = Path(note["installed_path"])
    else:
        run = running.get(plugin_id)
        candidate = run.path if run is not None and _same_dir(run.path.parent, live_root) else None
    if candidate is None:
        stray = live_root / plugin_id
        if _is_link(stray) and not stray.exists():
            # A dangling link at exactly <live>/<id> (the checkout it pointed at moved): it has
            # no target, so unlinking it can't touch anything else — it goes, rather than
            # leaving the operator to `rm` it by hand.
            return stray, ""
        if stray.exists():
            return None, (
                f"{stray} doesn't hold a copy of {plugin_id!r} (it holds another plugin, or none) — "
                f"uninstall won't delete it. {plugin_id!r} ships with protoAgent; to turn it off, disable it."
            )
        return None, ""  # nothing on disk at all
    if not _same_dir(candidate.parent, live_root):
        return None, f"{candidate} is outside the live plugins dir {live_root} — refusing to delete it."
    manifest = load_manifest(candidate)
    if manifest is None or manifest.id != plugin_id:
        return None, f"{candidate} does not hold plugin {plugin_id!r} — refusing to delete it."
    return candidate, ""


def _is_link(path: Path) -> bool:
    """A symlink OR a Windows directory junction — the no-admin way to link a dev checkout
    (#2298). Either is UNLINKED, never followed: treated as a real folder, a junction was
    renamed aside, ``rmtree`` refused it, and ``ignore_errors`` left an inert ``.bak``.
    ``Path.is_junction`` is 3.12+, hence the ``getattr``. (Junction handling is untested
    on real Windows; the unit test simulates one.)"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        return bool(is_junction and is_junction())
    except OSError:
        return False


def _unlink_link(path: Path) -> None:
    """Remove the LINK itself. A junction is a directory reparse point: ``os.rmdir``
    removes it and never touches the files it points at (``unlink`` refuses one)."""
    if path.is_symlink():
        path.unlink()
    else:
        os.rmdir(path)


def _plain_copy_refusal(
    plugin_id: str, target: Path, live_root: Path, manifest: PluginManifest | None, *, dangling: bool
) -> str | None:
    """Why the ordinary uninstall must NOT delete ``target`` (``<live>/<id>``), else None.

    Since #3452 ``<live>`` is ``plugins.dir`` when set — often a folder of checkouts, not a
    folder only the installer writes — so "a folder is there" proves nothing. It goes only
    when it IS this plugin: a manifest with this id, or (no readable manifest) a
    ``plugins.lock`` row saying the installer put it there, so a broken install stays
    removable. Never from the bundled tree. A dangling link has no target, so unlinking
    it touches nothing else."""
    if not (target.exists() or _is_link(target)):
        return None  # nothing on disk — only the lock row / config refs to clear
    if _same_dir(bundled_plugins_dir(), live_root):
        return (
            f"{target} is in protoAgent's own bundled plugins tree (plugins.dir points there) — "
            "uninstall won't delete from it. Point plugins.dir somewhere else."
        )
    if dangling:
        return None
    if manifest is not None:
        if manifest.id == plugin_id:
            return None
        return f"{target} holds plugin {manifest.id!r}, not {plugin_id!r} — uninstall won't delete it."
    if plugin_id in _lock_rows_by_id():
        return None
    return (
        f"{target} holds no plugin and plugins.lock has no entry for {plugin_id!r}, so it isn't an "
        "install — uninstall won't delete it. If it's yours to remove, delete it by hand."
    )


def _discard(path: Path) -> None:
    """Best-effort removal of an install/uninstall swap leftover (``<id>.bak``). A
    link (symlink or junction) is unlinked, never followed — ``shutil.rmtree`` refuses
    links, and with ``ignore_errors`` that refusal used to leave the link behind silently."""
    try:
        if _is_link(path):
            _unlink_link(path)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        log.warning("[plugins] could not remove %s — delete it by hand", path, exc_info=True)


def _remove_installed_copy(target: Path) -> None:
    """Delete one installed plugin folder. A symlink (the #2298 live-checkout install) is
    UNLINKED — the checkout it points at is the developer's and is never touched. A real
    folder is renamed aside and then deleted (#3075), so an interruption leaves
    ``<id>.bak``, never a half-deleted live plugin — and discovery skips ``*.bak``, so a
    leftover can never load in place of anything."""
    if _is_link(target):  # re-checked HERE, at delete time — a folder swapped for a link since the guards ran
        _unlink_link(target)
        return
    backup = target.parent / (target.name + ".bak")
    _discard(backup)
    try:
        os.rename(target, backup)
    except OSError as exc:
        # Callers (the REST routes, the ops layer, the CLI) handle InstallError; a bare
        # OSError escaping from here is a 500 / traceback instead of "couldn't remove it".
        raise InstallError(
            f"could not remove the installed copy at {target} (it was left in place): {exc}"
        ) from exc
    _discard(backup)
    if backup.exists() or _is_link(backup):
        log.warning("[plugins] %s could not be fully deleted — it is inert (*.bak is never loaded); remove it by hand", backup)


def _running_copy_is(plugin_id: str, target: Path) -> bool:
    """True when THIS process imported ``plugin_id`` from ``target``: deleting ``target``
    would pull the files out from under running code, and the next lazy ``from . import x``
    in it would fail. Compares the live module's ``__path__``/``__file__`` with ``target``
    both as spelled and resolved (a symlinked copy imports through the link's path)."""
    import sys

    from graph.plugins.loader import _plugin_module_name

    module = sys.modules.get(_plugin_module_name(plugin_id))
    if module is None:
        return False

    def _forms(p: str) -> set[str]:
        out = {os.path.normcase(os.path.abspath(p))}
        try:
            out.add(os.path.normcase(str(Path(p).resolve())))
        except OSError:
            pass
        return out

    roots = _forms(str(target))
    for loc in [*(getattr(module, "__path__", None) or []), getattr(module, "__file__", None) or ""]:
        if not loc:
            continue
        for form in _forms(str(loc)):
            if any(form == r or form.startswith(r + os.sep) for r in roots):
                return True
    return False


def _validate_pip_specs(plugin_id: str, deps: list[str]) -> None:
    """Reject ``requires_pip`` entries that aren't plain package requirements — a
    pip option (``--index-url``/``-e``), a VCS/URL/direct reference, or junk — so a
    plugin manifest can't inject pip flags (index hijack) or arbitrary build code
    beyond the named packages an operator reviewed. ``--`` before the specs in the
    pip argv is the belt to this suspenders."""
    for d in deps:
        s = str(d).strip()
        low = s.lower()
        if not s or s.startswith("-"):
            raise InstallError(
                f"plugin {plugin_id!r}: requires_pip entry {d!r} looks like a pip option, not a package."
            )
        if "://" in s or "@" in s or low.startswith(("git+", "hg+", "svn+", "bzr+", "file:")):
            raise InstallError(
                f"plugin {plugin_id!r}: requires_pip entry {d!r} is a VCS/URL/direct reference, which is not allowed."
            )
        if not _PKG_NAME_RE.match(s):
            raise InstallError(
                f"plugin {plugin_id!r}: requires_pip entry {d!r} is not a valid PEP 508 package requirement."
            )


def recorded_source_url(plugin_id: str) -> str:
    """The plugin's install origin from ``plugins.lock``, or "" when untracked.

    Empty for a bundled/built-in plugin and for a hand-copied working-tree dir —
    neither has a recorded origin to re-validate, and both are the operator's own
    deliberate placement rather than a fetched source. (``_lock_entry`` skips the
    non-dict members a hand-edited lock can carry, and picks the same row as the loader.)"""
    return str((_lock_entry(plugin_id) or {}).get("source_url") or "")


def install_deps(plugin_id: str) -> list[str]:
    """Pip-install a plugin's declared ``requires_pip`` — the explicit code-exec
    step that ``install`` deliberately skips (ADR 0027 D4). Optional deps (#1953)
    ride along best-effort: a failed optional install warns instead of failing
    the command. Returns the deps actually installed/satisfied.

    Acts on the copy the loader RUNS (``effective_copies``) — not simply whichever
    folder exists: with a bundled copy superseding an old git install, the git copy's
    deps list is the wrong one, and installing it leaves the running plugin broken."""
    manifest = effective_copies().get(plugin_id)
    if manifest is None:
        raise InstallError(f"plugin {plugin_id!r} is not installed.")
    # Allowlist re-check at deps time (#2743): a plugin installed BEFORE the
    # operator tightened ``plugins.sources.allow`` could otherwise still pull its
    # declared deps — "was allowed then" must not imply "is allowed now" for a
    # code-adjacent step. Same predicate ``install`` enforces, against the CURRENT
    # allowlist; skipped when there is no recorded origin (bundled / working-tree).
    # The CONSENT half (source_trusted) deliberately lives in the operator route,
    # exactly where install's own consent gate lives — the CLI is the operator's
    # explicit act, and only the console flow can render the ack dialog. The origin is
    # the RUNNING copy's: a bundled copy (superseding or not) was never fetched.
    source_url = effective_source_url(plugin_id)
    if source_url and not _source_allowed(source_url, configured_allowlist()):
        raise InstallError(
            f"{plugin_id!r} was installed from {source_url!r}, which is no longer on "
            f"plugins.sources.allow — re-add the source or reinstall from an allowed origin "
            f"before installing its deps."
        )
    deps = list(manifest.requires_pip)
    optional = list(manifest.optional_pip)
    if not deps and not optional:
        return []
    _validate_pip_specs(plugin_id, [*deps, *optional])
    # Frozen runtime (desktop): the host has no pip. Deps already bundled / on the
    # wheel-deps path / in the managed runtime need nothing; anything still missing is
    # installed into whichever target is available, turning ADR 0058 D2's "install it on
    # a server instead" refusal into a one-click install. Two complementary targets:
    #   • ADR 0093 — pure-Python wheels into a host sys.path deps dir (opt-in
    #     `plugins.allow_unbundled_deps`), for deps the plugin's OWN module imports;
    #   • ADR 0094 P2 — into the managed Python runtime (when provisioned), for deps the
    #     plugin's execute_code SKILLS import.
    # A dep that does NOT declare a scope could be either kind, so we satisfy every
    # available target; at least one must succeed or we refuse, naming what to enable.
    # A `scope: host` dep (#2246) is unambiguous — it must land where THIS process can
    # import it, so the managed runtime doesn't count as satisfying it. Optional deps
    # only warn (#1953).
    if _frozen_like():
        scopes = manifest.pip_scopes
        ok, missing = _deps_satisfied(deps, scopes)
        _, soft_missing = _deps_satisfied(optional, scopes)
        if ok and not soft_missing:
            log.info("[plugins] %s deps already satisfied (bundled / wheel-deps / managed runtime)", plugin_id)
            return deps + optional
        to_install = [s for s in deps if _dep_pkg_name(s) in missing]
        to_install_soft = [s for s in optional if _dep_pkg_name(s) in soft_missing]
        targets_tried: list[str] = []
        errors: list[str] = []

        # Target 1 (ADR 0093): host wheel-deps dir, opt-in.
        if _allow_unbundled_deps():
            from graph.plugins import wheel_installer

            try:
                wheel_installer.install(
                    plugin_id,
                    to_install + to_install_soft,
                    already_satisfied=lambda n: _deps_satisfied([n], scopes)[0],
                )
                targets_tried.append("wheel-deps")
            except wheel_installer.WheelInstallError as exc:
                errors.append(f"wheel install: {exc}")

        # Target 2 (ADR 0094 P2): managed runtime, when provisioned. HOST-scoped deps are
        # withheld (#2246) — the runtime serves execute_code children, so installing an
        # in-process import there would report success while leaving it just as broken.
        runtime_specs = [s for s in to_install + to_install_soft if scopes.get(_dep_pkg_name(s)) != "host"]
        from runtime.python_install import PythonRuntimeError, install_requirements_into_managed_runtime

        if runtime_specs:
            try:
                install_requirements_into_managed_runtime(runtime_specs)
                targets_tried.append("managed-runtime")
            except PythonRuntimeError as exc:
                errors.append(f"managed runtime: {exc}")

        # A host-scoped dep is only really installed if THIS process can now import it.
        # Verify rather than trusting "some target accepted it" — believing the wrong
        # interpreter is the entire bug this scope exists to prevent.
        still_missing_host = [n for n in _deps_satisfied(deps, scopes)[1] if scopes.get(n) == "host"]
        if still_missing_host:
            raise InstallError(
                f"{plugin_id!r} needs {', '.join(still_missing_host)} importable in the HOST process "
                f"(scope: host), and a frozen app can't provide that: the managed Python runtime only "
                f"serves execute_code children, and pure-Python wheel deps require Settings ▸ Plugins ▸ "
                f"'Install unbundled plugin deps'. Enable that, vendor the code, or ship the dep in the "
                f"app bundle." + (f" [{'; '.join(errors)}]" if errors else "")
            )

        if not targets_tried:
            # Nothing installed anywhere. Hard deps are fatal; a bare optional gap degrades.
            hint = (
                f"{plugin_id!r} needs {', '.join(missing)} which the desktop runtime doesn't have. "
                "Enable Settings ▸ Plugins ▸ 'Install unbundled plugin deps' (pure-Python wheels) "
                "and/or provision the Python runtime (Settings ▸ Tools), then retry."
            )
            if to_install:
                raise InstallError(f"{hint} [{'; '.join(errors)}]" if errors else hint)
            # Optional-only gap degrades with a warning that NAMES the deps (#1953).
            # Only the MISSING optionals drop out of the return — already-satisfied
            # ones stay, or callers would report the plugin degraded when it isn't.
            log.warning(
                "[plugins] %s: optional dep(s) %s aren't in the desktop runtime — the plugin "
                "degrades without them (%s)",
                plugin_id,
                ", ".join(soft_missing),
                "; ".join(errors) or "no target",
            )
            return deps + [d for d in optional if _dep_pkg_name(d) not in soft_missing]
        _audit("install_deps", {"id": plugin_id, "deps": to_install, "targets": targets_tried}, "ok")
        return deps + [d for d in optional if _dep_pkg_name(d) not in soft_missing or d in to_install_soft]
    installed: list[str] = []
    if deps:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--", *deps],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            _audit("install_deps", {"id": plugin_id, "deps": deps}, "pip install failed", success=False)
            raise InstallError(f"pip install failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
        installed += deps
    if optional:
        # Best-effort (#1953): the plugin runs without these, so a failure is
        # audited + warned, never fatal — the hard deps above already landed.
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--", *optional],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            _audit(
                "install_deps", {"id": plugin_id, "optional": optional}, "optional pip install failed", success=False
            )
            log.warning(
                "[plugins] %s: optional dep install failed (continuing without): %s",
                plugin_id,
                (proc.stderr or proc.stdout).strip()[-400:],
            )
        else:
            installed += optional
    _audit("install_deps", {"id": plugin_id, "deps": installed}, f"installed {len(installed)} dep(s)")
    log.info("[plugins] installed %d dep(s) for %s", len(installed), plugin_id)
    return installed


def list_installed() -> list[dict]:
    """Inventory of installed plugins — **disk is the source of truth**.

    Enumerates every plugin actually present in the live plugins dir (the SAME dir
    the loader discovers, so "installed" == "loadable") and overlays its
    ``plugins.lock`` provenance:

    * on disk **and** in the lock → ``tracked: True`` — source + SHA known, so it can
      be update-checked (``check_updates``) and re-synced.
    * on disk, **no** lock entry → ``tracked: False`` — a hand-placed local/dev copy
      (gitignored ``config/plugins/<id>``). Surfaced, not hidden: an enabled plugin
      that was never `install()`-ed used to be invisible here while running fine,
      because this only ever read the lock.
    * in the lock but **missing** from disk → ``present: False`` — fresh checkout or
      deleted code; ``sync`` refetches it at its pinned SHA.
    """
    root = live_plugins_dir()
    lock_by_id = _lock_rows_by_id()
    out: list[dict] = []
    on_disk: set[str] = set()

    if root.exists():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or is_swap_leftover(child):
                continue
            manifest = load_manifest(child)
            if manifest is None:
                continue  # a dir without a manifest isn't a plugin (mirror the loader)
            pid = manifest.id
            on_disk.add(pid)
            locked = lock_by_id.get(pid)
            if locked is not None:
                out.append({**locked, "present": True, "tracked": True})
            else:
                out.append(
                    {
                        "id": pid,
                        "source_url": "",
                        "requested_ref": "",
                        "resolved_sha": "",
                        "installed_at": "",
                        "by": "local",
                        "present": True,
                        "tracked": False,
                    }
                )

    # Locked but gone from disk — keep visible so the UI can offer `sync`.
    for pid, locked in lock_by_id.items():
        if pid not in on_disk:
            out.append({**locked, "present": False, "tracked": True})

    # Flag every row whose copy the LOADER ignores in favour of the bundled one — the
    # resolver decides, so this covers both reasons: the bundled manifest supersedes the
    # row's source, or (no lock row) #1574 demoted it on version. Update paths skip such a
    # row and a UI can say why it's inert. The PLUGIN is present either way (it ships with
    # protoAgent), so the row never reads as "missing on disk — sync": sync can't fetch it
    # and nothing is missing. `copy_on_disk` keeps the disk truth about the copy itself.
    running = effective_copies()
    bundled_root, live_root = loader_roots()
    for row in out:
        run = running.get(str(row.get("id") or ""))
        if run is None or _same_dir(bundled_root, live_root) or not _same_dir(run.path.parent, bundled_root):
            continue
        row["superseded"] = True
        row["bundled_version"] = run.version
        row["copy_on_disk"] = bool(row.get("present"))
        row["present"] = True
    # Same answer for a row that was never an install: the ADR 0093 wheel-deps pins a
    # BUNDLED plugin gets are a lock row with no source_url and no folder of their own.
    # Nothing was fetched and nothing can be re-fetched (``sync`` says "present" too), so
    # the row must not read as "missing on disk — sync" forever.
    for row in out:
        if row.get("present") or row.get("source_url"):
            continue
        if _bundled_manifest(str(row.get("id") or "")) is not None:
            row["copy_on_disk"] = False
            row["present"] = True

    out.sort(key=lambda e: e.get("id", ""))
    return out


def _ls_remote_sha(source_url: str, ref: str) -> str:
    """Latest remote commit SHA for ``ref`` (or the default branch / HEAD when
    ``ref`` is empty) at ``source_url``, via ``git ls-remote``. TTL-cached per
    (source_url, ref) and bounded by a short timeout so the UI poll can't hang.

    Raises ``InstallError`` (git failure) or ``subprocess.TimeoutExpired`` — both
    treated as a non-fatal per-plugin error by ``check_updates``."""
    key = (source_url, ref or "")
    now = time.monotonic()
    hit = _lsremote_cache.get(key)
    if hit is not None and (now - hit[0]) < _LSREMOTE_TTL_S:
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
    _auth = _git_auth_env(source_url)
    out = _git(
        "ls-remote", source_url, *refspecs, timeout=_LSREMOTE_TIMEOUT_S, env={**os.environ, **_auth} if _auth else None
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


# A release tag per the ADR 0049 pin lifecycle — `v1.2.3` / `1.2.3`. Prereleases and
# anything fancier deliberately don't match (they fall back to the moving-ref compare).
_SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _semver_key(tag: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` for a release tag, or ``None`` if not one."""
    m = _SEMVER_TAG_RE.match((tag or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_compatible_upgrade(pinned: str, candidate: str) -> bool:
    """True when moving ``pinned`` → ``candidate`` stays inside the compatible range.

    Caret semantics, as npm/cargo define them: the boundary is the **leftmost non-zero**
    version component, not the major. That distinction is the whole point here — every plugin
    in this fleet is ``0.x``, where a MINOR bump is where breaking changes land
    (``0.7.0 → 0.7.9`` compatible, ``0.7.0 → 0.8.0`` not). Bounding on the major alone would
    treat every 0.x release as compatible, which is the same unbounded chase with extra steps.

    A bundle's release-tag pin is a FLOOR (see ``install_bundle``): the installer adopts newer
    member releases automatically, so this is the guard that keeps "automatically" from
    including a deliberately breaking one.
    """
    a, b = _semver_key(pinned), _semver_key(candidate)
    if a is None or b is None:
        return False
    if b < a:
        return False  # never downgrade
    if a[0] != 0:
        return a[0] == b[0]                     # 1.x → same major
    if a[1] != 0:
        return b[0] == 0 and a[1] == b[1]       # 0.x → same minor
    return b[0] == 0 and b[1] == 0              # 0.0.x → same patch series


def is_release_tag(ref: str) -> bool:
    """True when ``ref`` is a semver release tag (``v1.2.3`` / ``1.2.3``) — the pin
    shape whose update moves tag → NEWER tag instead of re-fetching the same ref."""
    return _semver_key(ref) is not None


# `git ls-remote --tags` cache — same TTL/timeout regime as _lsremote_cache, its own
# dict because the value is a {tag: sha} map, not a single sha.
_lstags_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _ls_remote_tags(source_url: str) -> dict[str, str]:
    """``{tag: commit_sha}`` for every tag at ``source_url`` (TTL-cached, one
    timeout-bounded ``ls-remote --tags``). An annotated tag lists twice — the bare
    ref (tag-object SHA) and the peeled ``<ref>^{}`` (commit SHA); the peeled one
    wins, mirroring _ls_remote_sha's compare semantics (ADR 0049)."""
    now = time.monotonic()
    hit = _lstags_cache.get(source_url)
    if hit is not None and (now - hit[0]) < _LSREMOTE_TTL_S:
        return hit[1]
    _auth = _git_auth_env(source_url)  # authenticate the update-check for a private github repo (#1805 parity)
    out = _git(
        "ls-remote", "--tags", source_url, timeout=_LSREMOTE_TIMEOUT_S, env={**os.environ, **_auth} if _auth else None
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
    pid = entry.get("id", "")
    source_url = entry.get("source_url", "")
    requested_ref = entry.get("requested_ref", "") or ""
    current_sha = entry.get("resolved_sha", "") or ""

    pinned = bool(_SHA_RE.match(requested_ref))
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
    bundled = bundled_superseding(str(pid), str(source_url))
    if bundled is not None:
        result["superseded"] = True  # the bundled version rides the inventory row, not here
        return result
    if pinned or not source_url:
        if not source_url:
            result["error"] = "no source_url recorded — cannot check for updates"
        return result

    tag_key = _semver_key(requested_ref)
    try:
        if tag_key is not None:
            tags = _ls_remote_tags(source_url)
            newest_key, newest = tag_key, None
            for name, sha in tags.items():
                k = _semver_key(name)
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
            latest = _ls_remote_sha(source_url, requested_ref)
    except subprocess.TimeoutExpired:
        result["error"] = f"ls-remote timed out after {_LSREMOTE_TIMEOUT_S:.0f}s"
        return result
    except InstallError as exc:
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
    return [check_plugin_update(e) for e in _lock_rows_by_id().values()]


# ── Bundle-level lifecycle (ADR 0049 D4, #2718) ────────────────────────────────
# A bundle was first-class at install and never again: `check_updates`/`sync` read
# lock["plugins"] only, and uninstall had no bundle notion — so a published archetype repo's
# pin never moved on an installed host, and removing one meant hand-uninstalling
# members against a provenance row that slowly went stale.


def bundle_entry(bundle_id: str) -> dict | None:
    """The ``lock["bundles"]`` row for ``bundle_id`` (None when not installed)."""
    return next((b for b in _read_lock().get("bundles") or [] if b.get("id") == bundle_id), None)


def check_bundle_updates() -> list[dict]:
    """Bundle-level update status. A bundle lock row carries the same
    ``{id, source_url, requested_ref, resolved_sha}`` shape as a plugin row, so each
    rides ``check_plugin_update`` unchanged — ``behind`` means the bundle REPO moved
    (its member pins may have moved with it; ``ops.plugins.update_bundle``
    re-resolves them). Same pinned/release-tag/TTL semantics as plugins."""
    return [check_plugin_update(b) for b in _read_lock().get("bundles") or []]


def _bundle_ownership(bundle_id: str) -> tuple[dict | None, set[str], dict[str, str]]:
    """The shared ownership scan (extracted per the 2732 review): the bundle's lock
    row, the member ids every OTHER bundle row lists, and each installed plugin's
    ``by`` provenance. The rule everywhere: a member is exclusively this bundle's
    iff it still carries ``by == bundle:<id>`` and no other row lists it."""
    lock = _read_lock()
    row = next((b for b in lock.get("bundles") or [] if b.get("id") == bundle_id), None)
    listed_elsewhere: set[str] = set()
    for b in lock.get("bundles") or []:
        if b.get("id") != bundle_id:
            listed_elsewhere.update(str(p) for p in b.get("plugins") or [])
    by_of = {str(e.get("id")): e.get("by", "") for e in lock.get("plugins") or []}
    return row, listed_elsewhere, by_of


def _exclusively_owned(pid: str, bundle_id: str, listed_elsewhere: set[str], by_of: dict[str, str]) -> bool:
    return pid not in listed_elsewhere and by_of.get(pid, "") == f"bundle:{bundle_id}"


def exclusive_bundle_members(bundle_id: str) -> list[str]:
    """The bundle's members owned ONLY by it: still carrying this bundle's ``by``
    provenance in ``lock["plugins"]`` and not listed by any other bundle row. These
    are what ``uninstall_bundle`` removes — a member another bundle lists, or one the
    operator re-installed directly since (its ``by`` moved), stays."""
    row, listed_elsewhere, by_of = _bundle_ownership(bundle_id)
    if row is None:
        return []
    return [
        str(pid) for pid in row.get("plugins") or [] if _exclusively_owned(str(pid), bundle_id, listed_elsewhere, by_of)
    ]


def orphaned_bundle_members(bundle_id: str, before_members: list[str]) -> list[str]:
    """After a bundle update rewrote its lock row: the members the NEW manifest
    dropped that are still exclusively this bundle's (by-provenance, not listed by
    any other bundle). The update path uninstalls these so a manifest that removed a
    member doesn't leave it installed forever with dangling provenance."""
    row, listed_elsewhere, by_of = _bundle_ownership(bundle_id)
    current = {str(p) for p in (row.get("plugins") or [])} if row else set()
    return [
        str(pid)
        for pid in before_members
        if pid not in current and _exclusively_owned(str(pid), bundle_id, listed_elsewhere, by_of)
    ]


def uninstall_bundle(bundle_id: str, *, purge: bool = False) -> dict:
    """Remove a bundle: uninstall its exclusively-owned members and drop the lock
    row. Shared members (listed by another bundle) and members whose ``by``
    provenance moved (re-installed directly) are kept; a member already gone from
    disk+lock (uninstalled individually — the provenance row still listed it) is
    skipped, not an error. ``purge`` forwards to each member uninstall (config +
    secrets removal)."""
    row, listed_elsewhere, by_of = _bundle_ownership(bundle_id)
    if row is None:
        raise BundleNotInstalledError(f"bundle {bundle_id!r} is not installed.")
    # Bucket every row member honestly (the 2732 review's bucketing finding: a member
    # uninstalled individually earlier — the stale-provenance case this function
    # exists for — landed in "kept" as if it were still installed and shared):
    #   no lock entry at all  → already gone         → skipped_missing
    #   exclusively ours      → uninstall            → removed_members (or failed{pid: why})
    #   anything else         → shared / re-owned    → kept
    removed_members: list[str] = []
    superseded: list[str] = []
    superseded_was_loaded: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    kept: list[str] = []
    for raw in row.get("plugins") or []:
        pid = str(raw)
        if pid not in by_of:
            skipped.append(pid)  # provenance row outlived the member — nothing to remove
        elif _exclusively_owned(pid, bundle_id, listed_elsewhere, by_of):
            try:
                report = uninstall(pid, purge=purge)
                if isinstance(report, dict) and report.get("superseded_by_bundled"):
                    # Only the member's IGNORED copy went — the plugin now ships with
                    # protoAgent and its bundled copy keeps running (still enabled). Not a
                    # removed member: callers must not unload it or report it gone.
                    superseded.append(pid)
                    if report.get("was_loaded"):
                        superseded_was_loaded.append(pid)
                else:
                    removed_members.append(pid)
            except InstallError as exc:
                # An uninstall that RAISED is not "already gone" — reporting it in
                # skipped_missing mislabeled a real failure (2740 review nit).
                failed[pid] = str(exc)
        else:
            kept.append(pid)
    # Re-read — each member uninstall rewrote the lock — then drop the row itself.
    lock = _read_lock()
    lock["bundles"] = [b for b in lock.get("bundles") or [] if b.get("id") != bundle_id]
    _write_lock(lock)
    _audit(
        "uninstall-bundle",
        {"id": bundle_id, "purge": purge},
        f"uninstalled bundle {bundle_id} ({len(removed_members)} member(s), {len(kept)} kept)",
    )
    log.info(
        "[plugins] uninstalled bundle %s — removed: %s; kept (shared/re-owned): %s; superseded copies removed "
        "(bundled copy keeps running): %s",
        bundle_id,
        ", ".join(removed_members) or "none",
        ", ".join(kept) or "none",
        ", ".join(superseded) or "none",
    )
    return {
        "id": bundle_id,
        "removed_members": removed_members,
        "skipped_missing": skipped,
        # Members whose uninstall RAISED (a race, a refusal) — distinct from
        # skipped_missing so callers never label a failure "already gone".
        "failed": failed,
        "kept": kept,
        # Members that moved into core: their ignored git copy was removed, the bundled
        # copy keeps running. `superseded_was_loaded` is the subset this process was still
        # running from the removed copy (upgraded under a live server) — unload those.
        "superseded": superseded,
        "superseded_was_loaded": superseded_was_loaded,
        "purged": purge,
    }


def sync(*, allow: list[str] | None = None) -> list[dict]:
    """Re-clone every locked plugin at its pinned SHA (reproducible install set).
    Missing ones are fetched; present ones are left as-is. A missing one whose source a
    bundled plugin now supersedes isn't fetched (``status: superseded``) — the bundled
    copy is what would load anyway.

    A row with no ``source_url`` isn't an install at all — e.g. the ADR 0093 wheel-deps
    pins a BUNDLED plugin gets — so there is nothing to re-clone: ``present`` when
    protoAgent ships that id, else ``failed`` with the reason (it used to KeyError)."""
    results = []
    root = live_plugins_dir()
    for pid, e in _lock_rows_by_id().items():
        if (root / pid).exists():
            results.append({"id": pid, "status": "present"})
            continue
        source_url = str(e.get("source_url") or "")
        if bundled_superseding(pid, source_url) is not None:
            results.append({"id": pid, "status": "superseded"})
            continue
        if not source_url:
            if _bundled_manifest(pid) is not None:
                results.append({"id": pid, "status": "present"})
            else:
                results.append({"id": pid, "status": "failed", "error": "no source_url recorded — nothing to re-fetch"})
            continue
        try:
            install(
                source_url,
                e.get("resolved_sha") or e.get("requested_ref") or None,
                force=True,
                by="sync",
                allow=allow,
            )
            results.append({"id": pid, "status": "installed"})
        except InstallError as exc:
            results.append({"id": pid, "status": "failed", "error": str(exc)})
    return results
