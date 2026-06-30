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

from graph.plugins.manifest import PluginManifest, load_manifest

log = logging.getLogger(__name__)


def bundled_plugins_dir() -> Path:
    """In-tree bundled (built-in) plugins root — ``app_root/plugins``. Resolved at
    call time so the env (PyInstaller _MEIPASS) is honored, never import-time."""
    return instance_paths().app_root / "plugins"


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


def live_plugins_dir() -> Path:
    """Where git-installed plugins land — the live dir the loader discovers
    (``instance_paths().plugins_dir``, honoring ``PROTOAGENT_PLUGINS_DIR``)."""
    return instance_paths().plugins_dir


def _git(*args: str, cwd: Path | None = None, timeout: float | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise InstallError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _validate_url(url: str) -> None:
    if not any(url.startswith(s) for s in _ALLOWED_SCHEMES):
        raise InstallError(f"unsupported source {url!r} — use https://, ssh://, git@, or a local path.")


def _validate_ref(ref: str) -> None:
    """Reject a ref that could escape the GitHub API URL path / inject a query, or
    reach git as an option. Empty = the default branch (resolved separately)."""
    if ".." in ref or not _REF_RE.match(ref):
        raise InstallError(f"invalid ref {ref!r} — use a branch, tag, or commit SHA.")


def _source_allowed(url: str, allow: list[str] | None) -> bool:
    """Optional fork lock-down (ADR 0027 D3): if an allowlist is configured, the
    URL must match one of its host/org globs (e.g. ``github.com/protoLabsAI/*``)."""
    if not allow:
        return True
    import fnmatch

    norm = re.sub(r"^(https?://|git://|ssh://|git@)", "", url).replace(":", "/")
    return any(fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, pat + "*") for pat in allow)


def _read_lock() -> dict:
    lock = lock_path()
    if lock.exists():
        try:
            return json.loads(lock.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("[plugins] %s is unreadable — starting a fresh lock", lock)
    return {"plugins": []}


def _write_lock(data: dict) -> None:
    data["plugins"].sort(key=lambda e: e.get("id", ""))
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps(data, indent=2) + "\n")


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


def configured_allowlist() -> list[str] | None:
    """`plugins.sources.allow` read from the live config file (for the CLI, which
    runs without a loaded LangGraphConfig). None = open."""
    try:
        import yaml

        from graph.config_io import config_yaml_path

        cfg_path = config_yaml_path()
        if not cfg_path.exists():
            return None
        data = yaml.safe_load(cfg_path.read_text()) or {}
        allow = (((data.get("plugins") or {}).get("sources") or {}).get("allow")) or None
        return [str(x) for x in allow] if allow else None
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
        "min_protoagent_version": m.min_protoagent_version,
        # what it contributes — surfaced in the install review (ADR 0027 D3)
        "contributes": {
            "tools": bool(m.config_section),  # heuristic; real tool list needs import
            "views": [v.get("label") for v in m.views],
            "secrets": m.secrets,
            "settings": [s.get("key") for s in m.settings],
        },
    }


def _clone(url: str, ref: str | None, dest: Path) -> str:
    """Clone ``url`` at ``ref`` into ``dest``; return the resolved commit SHA."""
    if ref and _SHA_RE.match(ref):
        # A specific commit: full clone (shallow can't reliably check out an
        # arbitrary SHA), then check it out.
        _git("clone", "--no-recurse-submodules", url, str(dest), timeout=_CLONE_TIMEOUT_S)
        _git("checkout", ref, cwd=dest)
    elif ref:
        # A tag or branch: shallow clone of just that ref.
        _git("clone", "--depth", "1", "--no-recurse-submodules", "--branch", ref, url, str(dest), timeout=_CLONE_TIMEOUT_S)
    else:
        _git("clone", "--depth", "1", "--no-recurse-submodules", url, str(dest), timeout=_CLONE_TIMEOUT_S)
    return _git("rev-parse", "HEAD", cwd=dest)


# --- Git-less fetch for the frozen desktop app (ADR 0058 D1) ---------------
# The frozen PyInstaller sidecar has no `git` (and no `pip`), but the loader
# already discovers + importlib-loads plugins from the live root in frozen mode.
# So the only gap is *fetching* the code: download a GitHub archive tarball over
# HTTPS (the bundled httpx) and extract it — an on-disk result identical to a
# shallow clone. `git` stays the path on a dev/server box (history, ssh, private
# auth); the archive path is preferred when git is absent or we're frozen.

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


def _deps_satisfied(deps: list[str]) -> tuple[bool, list[str]]:
    """(all importable?, [missing dist names]) for a plugin's ``requires_pip``."""
    missing = [n for spec in deps if (n := _dep_pkg_name(spec)) and not _importable(n)]
    return (not missing, missing)


def install(
    url: str, ref: str | None = None, *, force: bool = False, by: str = "cli", allow: list[str] | None = None
) -> dict:
    """Clone a plugin from ``url`` (at ``ref``) into the live plugins dir, pinned
    to its resolved SHA, and record it in ``plugins.lock``. Does NOT enable it or
    install its deps. Returns the install summary."""
    _validate_url(url)
    if ref:
        _validate_ref(ref)  # before it reaches git or the GitHub API URL (PR #1140 QA)
    if not _source_allowed(url, allow):
        raise InstallError(
            f"source {url!r} is not on plugins.sources.allow — add it or install from an allowed origin."
        )

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

        # No silent shadowing of a built-in (repo) plugin.
        if (bundled_plugins_dir() / pid).exists():
            raise InstallError(f"plugin id {pid!r} is a built-in — cannot install over it.")

        # Frozen runtime (desktop): no pip — a plugin can only run if its declared
        # deps are already importable in the bundle. Refuse early with a clear
        # message instead of a cryptic enable-time ImportError (ADR 0058 D2).
        if _frozen_like() and manifest.requires_pip:
            ok, missing = _deps_satisfied(manifest.requires_pip)
            if not ok:
                raise InstallError(
                    f"{pid!r} needs {', '.join(missing)} which isn't in the desktop runtime — "
                    f"install it on a server/Docker build instead."
                )

        target = target_root / pid
        if target.exists():
            if not force:
                raise InstallError(f"plugin {pid!r} already installed — use --force to replace.")
            shutil.rmtree(target)

        shutil.rmtree(staging / ".git", ignore_errors=True)  # drop git metadata; lock holds provenance
        shutil.move(str(staging), str(target))
        manifest = load_manifest(target) or manifest  # re-read from final path

    summary = _summary(manifest, source=url, ref=ref or "", sha=sha)
    lock = _read_lock()
    lock["plugins"] = [e for e in lock["plugins"] if e.get("id") != pid]
    lock["plugins"].append(
        {
            "id": pid,
            "source_url": url,
            "requested_ref": ref or "",
            "resolved_sha": sha,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "by": by,
        }
    )
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


def _install_bundle(
    bundle: dict, bundle_url: str, bundle_sha: str, ref: str | None, *, force: bool, by: str, allow: list[str] | None
) -> dict:
    """Install every plugin a bundle names (reusing single-plugin ``install()`` for
    each — so each member is allow-checked + pinned in ``plugins.lock`` exactly as a
    direct install), then record the bundle for provenance. Enable + config are
    *suggested* in the return value, never applied (install ≠ enable ≠ trust)."""
    bid = str(bundle.get("id"))
    installed: list[dict] = []
    skipped: list[str] = []
    for entry in bundle.get("plugins") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("builtin"):
            skipped.append(str(entry.get("id", "?")))  # ships with protoAgent — nothing to fetch
            continue
        purl = entry.get("url")
        if not purl:
            raise InstallError(f"bundle {bid!r}: plugin {entry.get('id', '?')!r} has no url")
        installed.append(install(str(purl), entry.get("ref"), force=force, by=f"bundle:{bid}", allow=allow))

    lock = _read_lock()
    lock.setdefault("bundles", [])
    lock["bundles"] = [b for b in lock["bundles"] if b.get("id") != bid]
    lock["bundles"].append(
        {
            "id": bid,
            "source_url": bundle_url,
            "requested_ref": ref or "",
            "resolved_sha": bundle_sha,
            "plugins": [s["id"] for s in installed],
            # The bundle's curated turn-on list (a subset of `plugins`). Cached here so a
            # consumer that only sees the lock — e.g. the fleet new-agent path, which
            # installs via a CLI subprocess and never sees the live install summary — can
            # auto-enable exactly what the bundle author intended. Empty = enable all members.
            "enabled": list(bundle.get("enabled") or []),
            # The bundle's recommended per-plugin config defaults ({section: {key: val}}).
            # Cached for the same lock-only consumer; applied as DEFAULTS (operator values
            # win, present keys are never clobbered — see `bundle_config_overlay`).
            "config": dict(bundle.get("config") or {}),
            # Archetype metadata (ADR 0042) cached here so the new-agent picker can offer
            # this bundle as a starter type without re-reading its manifest.
            "archetype": bundle.get("archetype") or {},
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
    return {
        "bundle": bid,
        "name": bundle.get("name", ""),
        "description": bundle.get("description", ""),
        "resolved_sha": bundle_sha,
        "installed": installed,
        "skipped_builtin": skipped,
        "enabled": list(bundle.get("enabled") or []),
        "config": bundle.get("config") or {},
    }


def _clean_config_refs(plugin_id: str, section: str, purge: bool) -> bool:
    """Remove the plugin's references from the live langgraph-config.yaml (ADR 0027):
    always the `plugins.enabled`/`disabled` entry (a dangling enabled entry is just
    broken); with ``purge`` also the plugin's `config_section` block. Comment-safe
    (ruamel). Returns True if anything changed."""
    from graph.config_io import config_yaml_path, load_yaml_doc, save_yaml_doc

    cfg = config_yaml_path()
    if not cfg.exists():
        return False
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
    from graph.config_io import load_yaml_doc, save_yaml_doc, secrets_yaml_path

    sec = secrets_yaml_path()
    if not sec.exists():
        return False
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
    returned for the operator to remove. Returns a report dict."""
    if (bundled_plugins_dir() / plugin_id).exists():
        raise InstallError(f"{plugin_id!r} is a built-in plugin — not removable via uninstall.")
    target = live_plugins_dir() / plugin_id
    # Read the manifest BEFORE deleting — purge needs the config section + we report
    # the declared deps.
    manifest = load_manifest(target) if (target / "protoagent.plugin.yaml").exists() else None
    section = (manifest.config_section if manifest else "") or plugin_id
    deps_left = list(manifest.requires_pip) if manifest else []

    removed: list[str] = []
    if target.exists():
        shutil.rmtree(target)
        removed.append("code")
    lock = _read_lock()
    before = len(lock["plugins"])
    lock["plugins"] = [e for e in lock["plugins"] if e.get("id") != plugin_id]
    if len(lock["plugins"]) != before:
        _write_lock(lock)
        removed.append("lock")
    if _clean_config_refs(plugin_id, section, purge):
        removed.append("config" if purge else "enabled-ref")
    if purge and _clean_secrets(section):
        removed.append("secrets")

    if not removed:
        raise InstallError(f"plugin {plugin_id!r} is not installed.")
    _audit("uninstall", {"id": plugin_id, "purge": purge}, f"uninstalled {plugin_id} ({', '.join(removed)})")
    log.info("[plugins] uninstalled %s (%s)", plugin_id, ", ".join(removed))
    return {"id": plugin_id, "removed": removed, "deps_left": deps_left, "purged": purge}


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


def install_deps(plugin_id: str) -> list[str]:
    """Pip-install a plugin's declared ``requires_pip`` — the explicit code-exec
    step that ``install`` deliberately skips (ADR 0027 D4). Returns the deps."""
    manifest = None
    for base in (live_plugins_dir(), bundled_plugins_dir()):
        if (base / plugin_id / "protoagent.plugin.yaml").exists():
            manifest = load_manifest(base / plugin_id)
            break
    if manifest is None:
        raise InstallError(f"plugin {plugin_id!r} is not installed.")
    deps = list(manifest.requires_pip)
    if not deps:
        return []
    _validate_pip_specs(plugin_id, deps)
    # Frozen runtime (desktop): no pip. The deps must already be bundled — confirm
    # (nothing to install) or refuse with a clear message (ADR 0058 D2).
    if _frozen_like():
        ok, missing = _deps_satisfied(deps)
        if not ok:
            raise InstallError(
                f"{plugin_id!r} needs {', '.join(missing)} which isn't in the desktop runtime — "
                f"install it on a server/Docker build instead."
            )
        log.info("[plugins] %s deps already in the runtime — nothing to install", plugin_id)
        return deps
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--", *deps],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        _audit("install_deps", {"id": plugin_id, "deps": deps}, "pip install failed", success=False)
        raise InstallError(f"pip install failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
    _audit("install_deps", {"id": plugin_id, "deps": deps}, f"installed {len(deps)} dep(s)")
    log.info("[plugins] installed %d dep(s) for %s", len(deps), plugin_id)
    return deps


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
    lock_by_id = {e["id"]: e for e in _read_lock()["plugins"] if e.get("id")}
    out: list[dict] = []
    on_disk: set[str] = set()

    if root.exists():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
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
    out = _git("ls-remote", source_url, *refspecs, timeout=_LSREMOTE_TIMEOUT_S)
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


def check_plugin_update(entry: dict) -> dict:
    """Update status for one ``plugins.lock`` entry. A *pinned* plugin (its
    ``requested_ref`` is a full/abbrev commit SHA per ``_SHA_RE``) never
    auto-updates — we skip the network call entirely. Otherwise compare the
    stored ``resolved_sha`` against the latest remote SHA for its ref. Any
    network/timeout/lookup failure is reported in ``error`` (non-fatal)."""
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
        "behind": False,
        "pinned": pinned,
        "error": None,
    }
    if pinned or not source_url:
        if not source_url:
            result["error"] = "no source_url recorded — cannot check for updates"
        return result

    try:
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
    timeout-bounded) and report ``behind``. Network errors are non-fatal per entry."""
    return [check_plugin_update(e) for e in _read_lock()["plugins"]]


def sync(*, allow: list[str] | None = None) -> list[dict]:
    """Re-clone every locked plugin at its pinned SHA (reproducible install set).
    Missing ones are fetched; present ones are left as-is."""
    results = []
    root = live_plugins_dir()
    for e in _read_lock()["plugins"]:
        pid = e["id"]
        if (root / pid).exists():
            results.append({"id": pid, "status": "present"})
            continue
        try:
            install(
                e["source_url"],
                e.get("resolved_sha") or e.get("requested_ref") or None,
                force=True,
                by="sync",
                allow=allow,
            )
            results.append({"id": pid, "status": "installed"})
        except InstallError as exc:
            results.append({"id": pid, "status": "failed", "error": str(exc)})
    return results
