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

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from graph.config_io import _BUNDLE_CONFIG_DIR, _live_config_dir
from graph.plugins.manifest import PluginManifest, load_manifest

log = logging.getLogger(__name__)

REPO_ROOT = _BUNDLE_CONFIG_DIR.parent
LOCK_PATH = Path(os.environ.get("PROTOAGENT_PLUGINS_LOCK", str(REPO_ROOT / "plugins.lock")))

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_ALLOWED_SCHEMES = ("https://", "http://", "git://", "ssh://", "git@", "file://", "/")


class InstallError(RuntimeError):
    """A plugin install/uninstall/sync failed (bad URL, manifest, git, collision)."""


def live_plugins_dir() -> Path:
    """Where git-installed plugins land — the live dir the loader discovers."""
    override = os.environ.get("PROTOAGENT_PLUGINS_DIR", "")
    return Path(override).expanduser() if override else (_live_config_dir() / "plugins")


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise InstallError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _validate_url(url: str) -> None:
    if not any(url.startswith(s) for s in _ALLOWED_SCHEMES):
        raise InstallError(
            f"unsupported source {url!r} — use https://, ssh://, git@, or a local path."
        )


def _source_allowed(url: str, allow: list[str] | None) -> bool:
    """Optional fork lock-down (ADR 0027 D3): if an allowlist is configured, the
    URL must match one of its host/org globs (e.g. ``github.com/protoLabsAI/*``)."""
    if not allow:
        return True
    import fnmatch
    norm = re.sub(r"^(https?://|git://|ssh://|git@)", "", url).replace(":", "/")
    return any(fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, pat + "*") for pat in allow)


def _read_lock() -> dict:
    if LOCK_PATH.exists():
        try:
            return json.loads(LOCK_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("[plugins] %s is unreadable — starting a fresh lock", LOCK_PATH)
    return {"plugins": []}


def _write_lock(data: dict) -> None:
    data["plugins"].sort(key=lambda e: e.get("id", ""))
    LOCK_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _audit(action: str, args: dict, summary: str, *, success: bool = True) -> None:
    """Record install/uninstall/install-deps to the audit log (ADR 0027 D5)."""
    try:
        from audit import audit_logger
        audit_logger.log(
            session_id="plugins", tool=f"plugin.{action}", args=args,
            result_summary=summary, duration_ms=0, success=success,
        )
    except Exception:  # noqa: BLE001 — auditing must never block the operation
        log.debug("[plugins] audit log failed for %s", action, exc_info=True)


def configured_allowlist() -> list[str] | None:
    """`plugins.sources.allow` read from the live config file (for the CLI, which
    runs without a loaded LangGraphConfig). None = open."""
    try:
        import yaml
        cfg_path = _live_config_dir() / "langgraph-config.yaml"
        if not cfg_path.exists():
            return None
        data = yaml.safe_load(cfg_path.read_text()) or {}
        allow = (((data.get("plugins") or {}).get("sources") or {}).get("allow")) or None
        return [str(x) for x in allow] if allow else None
    except Exception:  # noqa: BLE001
        return None


def _summary(m: PluginManifest, *, source: str, ref: str, sha: str) -> dict:
    return {
        "id": m.id, "name": m.name, "version": m.version, "description": m.description,
        "source_url": source, "requested_ref": ref, "resolved_sha": sha,
        "repository": m.repository, "homepage": m.homepage,
        "capabilities": m.capabilities, "requires_env": m.requires_env,
        "requires_pip": m.requires_pip, "min_protoagent_version": m.min_protoagent_version,
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
        _git("clone", "--no-recurse-submodules", url, str(dest))
        _git("checkout", ref, cwd=dest)
    elif ref:
        # A tag or branch: shallow clone of just that ref.
        _git("clone", "--depth", "1", "--no-recurse-submodules", "--branch", ref, url, str(dest))
    else:
        _git("clone", "--depth", "1", "--no-recurse-submodules", url, str(dest))
    return _git("rev-parse", "HEAD", cwd=dest)


def install(url: str, ref: str | None = None, *, force: bool = False,
            by: str = "cli", allow: list[str] | None = None) -> dict:
    """Clone a plugin from ``url`` (at ``ref``) into the live plugins dir, pinned
    to its resolved SHA, and record it in ``plugins.lock``. Does NOT enable it or
    install its deps. Returns the install summary."""
    _validate_url(url)
    if not _source_allowed(url, allow):
        raise InstallError(
            f"source {url!r} is not on plugins.sources.allow — add it or install from an allowed origin."
        )

    target_root = live_plugins_dir()
    target_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pa-plugin-") as tmp:
        staging = Path(tmp) / "repo"
        sha = _clone(url, ref, staging)

        manifest = load_manifest(staging)
        if manifest is None:
            raise InstallError(
                f"{url!r} has no valid protoagent.plugin.yaml — not a protoAgent plugin."
            )
        pid = manifest.id

        # No silent shadowing of a built-in (repo) plugin.
        if (REPO_ROOT / "plugins" / pid).exists():
            raise InstallError(f"plugin id {pid!r} is a built-in — cannot install over it.")

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
    lock["plugins"].append({
        "id": pid, "source_url": url, "requested_ref": ref or "",
        "resolved_sha": sha, "installed_at": datetime.now(timezone.utc).isoformat(), "by": by,
    })
    _write_lock(lock)
    _audit("install", {"url": url, "ref": ref or "", "sha": sha, "id": pid},
           f"installed {pid}@{sha[:10]}")
    log.info("[plugins] installed %s@%s from %s", pid, sha[:10], url)
    return summary


def _clean_config_refs(plugin_id: str, section: str, purge: bool) -> bool:
    """Remove the plugin's references from the live langgraph-config.yaml (ADR 0027):
    always the `plugins.enabled`/`disabled` entry (a dangling enabled entry is just
    broken); with ``purge`` also the plugin's `config_section` block. Comment-safe
    (ruamel). Returns True if anything changed."""
    from graph.config_io import load_yaml_doc, save_yaml_doc
    cfg = _live_config_dir() / "langgraph-config.yaml"
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
    from graph.config_io import load_yaml_doc, save_yaml_doc
    sec = _live_config_dir() / "secrets.yaml"
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
    if (REPO_ROOT / "plugins" / plugin_id).exists():
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


def install_deps(plugin_id: str) -> list[str]:
    """Pip-install a plugin's declared ``requires_pip`` — the explicit code-exec
    step that ``install`` deliberately skips (ADR 0027 D4). Returns the deps."""
    manifest = None
    for base in (live_plugins_dir(), REPO_ROOT / "plugins"):
        if (base / plugin_id / "protoagent.plugin.yaml").exists():
            manifest = load_manifest(base / plugin_id)
            break
    if manifest is None:
        raise InstallError(f"plugin {plugin_id!r} is not installed.")
    deps = list(manifest.requires_pip)
    if not deps:
        return []
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", *deps], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _audit("install_deps", {"id": plugin_id, "deps": deps}, "pip install failed", success=False)
        raise InstallError(f"pip install failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
    _audit("install_deps", {"id": plugin_id, "deps": deps}, f"installed {len(deps)} dep(s)")
    log.info("[plugins] installed %d dep(s) for %s", len(deps), plugin_id)
    return deps


def list_installed() -> list[dict]:
    """Lock entries, annotated with whether the code is present on disk."""
    out = []
    root = live_plugins_dir()
    for e in _read_lock()["plugins"]:
        out.append({**e, "present": (root / e["id"]).exists()})
    return out


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
            install(e["source_url"], e.get("resolved_sha") or e.get("requested_ref") or None,
                    force=True, by="sync", allow=allow)
            results.append({"id": pid, "status": "installed"})
        except InstallError as exc:
            results.append({"id": pid, "status": "failed", "error": str(exc)})
    return results
