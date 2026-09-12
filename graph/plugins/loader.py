"""Discover and load drop-in plugins.

Two roots, mirroring config/skills: bundled (``<repo>/plugins/``, shipped
examples) and live (``<config_dir>/plugins/`` or ``plugins.dir``). A plugin is
loaded only when **enabled** — either ``enabled: true`` in its manifest (author
opt-in) or its id listed in ``plugins.enabled`` (operator opt-in). Enabled
plugins are imported and run **in-process**; everything is best-effort so one
bad plugin can't break the rest or the boot.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from graph.plugins.host import timed_lifecycle_phase
from graph.plugins.manifest import (
    PluginManifest,
    _iframe_page_route,
    display_source,
    is_swap_leftover,
    load_manifest,
    supersedes_source,
)
from graph.plugins.registry import PluginRegistry

log = logging.getLogger("protoagent.plugins")


@dataclass
class PluginLoadResult:
    tools: list = field(default_factory=list)
    # tool name -> the owning plugin's display name, so the console Tools tab can group
    # plugin tools by the plugin that contributed them instead of one flat "Plugin" bucket.
    tool_plugins: dict = field(default_factory=dict)
    skill_dirs: list = field(default_factory=list)
    workflow_dirs: list = field(default_factory=list)  # *.yaml recipe dirs (ADR 0027)
    goal_verifiers: dict = field(default_factory=dict)  # name -> verifier fn (ADR 0028)
    goal_verifier_meta: dict = field(default_factory=dict)  # name -> {plugin_id, description}
    work_providers: dict = field(default_factory=dict)  # name -> () -> list[dict] (ADR 0079)
    work_provider_meta: dict = field(default_factory=dict)  # name -> {plugin_id, label}
    goal_hooks: list = field(default_factory=list)  # {on_achieved, on_failed} (ADR 0028)
    watch_hooks: list = field(default_factory=list)  # {on_met, on_expired, on_stalled} (ADR 0067)
    lifecycle_hooks: list = field(default_factory=list)  # {on_app_loaded, on_agent_active, on_system_wake} (ADR 0074)
    knowledge_stores: dict = field(default_factory=dict)  # name -> backend factory (ADR 0031)
    embedders: dict = field(default_factory=dict)  # name -> embed_fn factory (ADR 0031)
    a2a_skills: list = field(default_factory=list)  # A2A card skill specs (#570)
    a2a_skill_plugins: dict = field(default_factory=dict)  # skill id -> plugin id (ownership, #2754)
    routers: list = field(default_factory=list)  # {plugin_id, router, prefix} (ADR 0018)
    public_paths: list = field(default_factory=list)  # manifest-declared auth-exempt prefixes
    federation_paths: list = field(default_factory=list)  # manifest-declared federation-tier prefixes (#2747)
    surfaces: list = field(default_factory=list)  # {plugin_id, name, start, stop}
    subagents: list = field(default_factory=list)  # SubagentConfig
    middleware: list = field(default_factory=list)  # factories: (config) -> AgentMiddleware|None (ADR 0032)
    late_tool_factories: list = field(default_factory=list)  # (all_tools, config) -> tool|list (late seam)
    mcp_servers: list = field(default_factory=list)  # factories: config -> entry|None (ADR 0019)
    thread_id_resolver: object = None  # (request_metadata, session_id) -> str (#571); last plugin wins
    chat_commands: dict = field(default_factory=dict)  # token -> handler; user-only chat control commands
    meta: list[dict] = field(default_factory=list)


def _version_key(v: str) -> tuple[int, int, int]:
    """Best-effort semver sort key: ``"0.14.0" → (0, 14, 0)``. A non-numeric part
    sorts as ``-1`` so a malformed version can't spuriously beat a real one."""
    parts: list[int] = []
    for p in str(v or "0").split(".")[:3]:
        m = re.match(r"\d+", p.strip())
        parts.append(int(m.group()) if m else -1)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _tracked_ids() -> set[str]:
    """Plugin ids recorded in ``plugins.lock`` — an INTENTIONAL install/override, vs
    an untracked hand-placed/leftover copy. Best-effort (empty on any error)."""
    return set(_tracked_sources())


def _tracked_sources() -> dict[str, str]:
    """``{plugin id: recorded source_url}`` for every ``plugins.lock`` entry — the ids
    (see ``_tracked_ids``) plus WHERE each copy was fetched from, which is what decides
    whether a bundled copy's ``supersedes`` retires it. Best-effort (empty on any error)."""
    try:
        from graph.plugins import installer

        # The installer's one row-per-id accessor, so the loader and uninstall can never
        # read different rows of a lock that lists an id twice.
        return {pid: str(e.get("source_url") or "") for pid, e in installer._lock_rows_by_id().items()}
    except Exception:  # noqa: BLE001
        return {}


# The setup-gap key the loader reports a superseded install under (see
# ``discover_plugins``). Host-owned; the installer clears it when the ignored copy goes.
SUPERSEDED_GAP_KEY = "superseded-install"


def discover_plugins(
    roots: list[Path],
    *,
    tracked_ids: set[str] | None = None,
    tracked_sources: dict[str, str] | None = None,
    superseded: dict[str, dict] | None = None,
) -> list[PluginManifest]:
    """Find plugins (dirs with a manifest) under *roots*, later roots (the live/installed
    dir) taking precedence over earlier ones (the bundled dir) by id — with three
    exceptions, checked in order:

    1. **Superseded** — the bundled copy declares ``supersedes: [<git URL>]`` and
       ``plugins.lock`` records the installed copy as fetched from one of those URLs: the
       plugin moved into core, so the BUNDLED copy wins at any version. (The same id is
       kept on purpose — a new one would orphan ``plugins.enabled``, the config section,
       and every archetype's enable list.) Each such decision lands in ``superseded``
       (``{id: {source_url, installed_version, installed_path, bundled_version}}``) when
       the caller passes a dict, so ``load_plugins`` can tell the operator.
    2. **Tracked** — any other copy recorded in ``plugins.lock`` (a fork, a deliberate
       pin) is an intentional override and wins at ANY version.
    3. **Untracked** — a copy not in the lock only wins when it's NOT OLDER than the
       bundled one (#1574). This stops a stale leftover from shadowing the bundled
       plugin: a plugin once git-installed (artifact @ 0.11.3) and later bundled in-tree
       at a newer version (0.14.0) would otherwise stay stuck on the old copy forever. A
       same-or-newer untracked copy (a dev override) still wins.

    A ``<id>.bak`` folder is the installer's transient swap copy (#3075), never a plugin,
    and is skipped — an interrupted install/uninstall must not leave a copy that loads.

    ``tracked_sources`` (id → recorded ``source_url``) defaults to ``plugins.lock``;
    ``tracked_ids`` alone marks ids as tracked with no known source (never superseded)."""
    if tracked_sources is None:
        tracked_sources = {pid: "" for pid in tracked_ids} if tracked_ids is not None else _tracked_sources()
    tracked = set(tracked_sources) | set(tracked_ids or ())
    by_id: dict[str, PluginManifest] = {}
    root_of: dict[str, int] = {}
    for index, root in enumerate(roots):
        if not (root and root.exists() and root.is_dir()):
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or is_swap_leftover(child):
                continue
            manifest = load_manifest(child)
            if manifest is None:
                continue
            incumbent = by_id.get(manifest.id)
            if incumbent is None:
                wins = True
            elif root_of[manifest.id] < index and supersedes_source(incumbent, tracked_sources.get(manifest.id)):
                # An earlier root (the bundled tree) declared it replaces the repo this
                # copy was installed from — the copy is retired, whatever its version.
                wins = False
                if superseded is not None:
                    superseded[manifest.id] = {
                        "source_url": tracked_sources.get(manifest.id, ""),
                        "installed_version": manifest.version,
                        "installed_path": str(manifest.path),
                        "bundled_version": incumbent.version,
                    }
            else:
                wins = manifest.id in tracked or _version_key(manifest.version) >= _version_key(incumbent.version)
            if wins:
                by_id[manifest.id] = manifest
                root_of[manifest.id] = index
    return list(by_id.values())


def _superseded_message(plugin_id: str, note: dict) -> str:
    """The operator-facing line for a superseded install — what happened, and the one
    action that clears it. Kept under the setup-gap cap (300 chars) for a normal URL."""
    return (
        f"now ships with protoAgent (v{note.get('bundled_version')}); the copy installed from "
        f"{display_source(note.get('source_url'))} (v{note.get('installed_version')}) is ignored. "
        f"Uninstall it in Settings ▸ Plugins or with `protoagent plugin uninstall {plugin_id}` — "
        "settings and enabled state are kept."
    )


def _entry_file(manifest: PluginManifest) -> Path | None:
    if manifest.entrypoint:
        candidate = manifest.path / manifest.entrypoint
        return candidate if candidate.exists() else None
    for name in ("__init__.py", "plugin.py"):
        candidate = manifest.path / name
        if candidate.exists():
            return candidate
    return None


def _plugin_module_name(plugin_id: str) -> str:
    """A valid Python module name for a plugin id. Non-identifier chars (e.g. the
    hyphen in ``finance-data``) become ``_`` — a hyphen in the module name breaks
    the relative-import machinery."""
    return "protoagent_plugin_" + re.sub(r"\W", "_", plugin_id)


# ── Re-exec avoidance (#3365) ─────────────────────────────────────────────────
# plugin id → (source fingerprint, entry module) for the generation currently
# live in ``sys.modules``. Re-executing a plugin's module tree does NOT free the
# previous generation: every function handed to ``register(registry)`` carries
# ``__globals__`` — the old module's ``__dict__`` — and third-party registries
# (pydantic model classes, SQLAlchemy annotation types) key off the classes each
# exec creates. Dropping the name from ``sys.modules`` frees none of that, so a
# process that rebuilt its graph on a cadence grew by a full copy of every plugin
# every time — ~6.4 MB per rebuild in a trimmed config, linear and unbounded,
# until the host ran out of memory.
#
# So: only re-exec a plugin whose sources actually moved. The fingerprint is the
# same one the code-drift banner uses, which is what makes this safe — a reload
# after an edit still re-execs, and the devkit's "edit then reload_plugins" loop
# is unaffected.
_LOADED_MODULES: dict[str, tuple[str, object]] = {}


def purge_plugin_modules(plugin_id: str) -> None:
    """Drop a plugin's module subtree from ``sys.modules`` so the next reload
    re-execs every file from disk. The loader re-execs the entry ``__init__`` each
    reload, but a multi-file plugin's ``from .tools import …`` resolves the SUBMODULE
    through ``sys.modules`` — which still holds the OLD code after a force
    re-install. Scoped to the plugin's own prefix; the reload rebuilds it. Shared by
    the console Update route and the auto-update loop (#1720)."""
    # Drop the re-exec cache too (#3365): a force re-install/update calls this to
    # guarantee fresh code, and must not be answered from the cache afterwards.
    _LOADED_MODULES.pop(plugin_id, None)
    prefix = _plugin_module_name(plugin_id)
    for name in [n for n in list(sys.modules) if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(name, None)


def _load_plugin_module(manifest: PluginManifest, entry: Path):
    """Import a plugin's entry ``__init__.py`` as a **package** so it can have
    sibling modules and use relative imports (``from .tools import …``). The
    module is registered in ``sys.modules`` BEFORE exec — relative imports resolve
    the parent package there — and the name is sanitized to a valid identifier.

    Re-exec is SKIPPED when the plugin's sources are byte-for-byte what they were
    at the last import (#3365) — see ``_LOADED_MODULES``. Re-running unchanged code
    leaked a full copy of the plugin and bought nothing.
    """
    mod_name = _plugin_module_name(manifest.id)
    # Same stamp the drift banner uses. Empty (unreadable tree) → never a cache hit,
    # so an unstampable plugin behaves exactly as it did before.
    fingerprint = _source_fingerprint(manifest.path)
    cached = _LOADED_MODULES.get(manifest.id)
    if (
        fingerprint
        and cached is not None
        and cached[0] == fingerprint
        # The live module must still be OURS: anything that purged or replaced it
        # out from under us invalidates the cache by definition.
        and sys.modules.get(mod_name) is cached[1]
    ):
        return cached[1]
    # Reload-safety: drop any cached modules for this plugin — the entry AND its sibling
    # submodules (``mod_name.*``) — so a hot-reload re-execs EVERY file, not just
    # __init__.py. Without this, ``from .tools import x`` resolves a stale cached
    # ``mod_name.tools`` and an edit to a sibling module silently has no effect until a
    # process restart (breaking the devkit's "edit then reload_plugins" loop). On a first
    # load this is a no-op (nothing cached). Shares the one purge implementation with
    # the console Update route and the auto-update loop (#1720).
    purge_plugin_modules(manifest.id)
    spec = importlib.util.spec_from_file_location(mod_name, str(entry), submodule_search_locations=[str(manifest.path)])
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # so `from .x import y` finds the parent
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    if fingerprint:
        _LOADED_MODULES[manifest.id] = (fingerprint, module)
    return module


def _import_register(manifest: PluginManifest):
    """Import a plugin's entry module and return its ``register`` callable."""
    entry = _entry_file(manifest)
    if entry is None:
        raise RuntimeError("no entry module (expected __init__.py or plugin.py)")
    module = _load_plugin_module(manifest, entry)
    _record_source_fingerprint(manifest)
    register = getattr(module, "register", None)
    if not callable(register):
        raise RuntimeError("plugin module has no callable register(registry)")
    return register


# ── Live-checkout code drift (#2298) ──────────────────────────────────────────
# A plugin installed as a SYMLINK to a working git checkout serves a *mix* of two
# versions when that checkout's branch changes under the running process. Python
# caches imported modules, so code imported before the change keeps running the old
# version — while anything imported LAZILY afterwards loads the new one. protoAgent
# plugins are written to import host-only deps lazily (``graph.*`` etc. are
# try-wrapped so a plugin's suite can run host-free), which makes them especially
# prone: the discipline that keeps plugins testable is exactly what lets a mid-flight
# branch switch produce a half-updated process. Nothing detected it, and the
# divergence is invisible from the outside.
#
# Only symlinked plugins are tracked. A normal install is an immutable copy the
# operator doesn't edit under a live process, so it pays nothing here.
_SOURCE_FINGERPRINTS: dict[str, tuple[str, str]] = {}


def _source_fingerprint(path: Path) -> str:
    """A cheap content-identity stamp for a plugin's Python sources.

    ``(relpath, mtime_ns, size)`` over every ``*.py``, hashed. A branch switch
    rewrites files, so mtimes move even when a file's size is unchanged. Deliberately
    not a content hash (that would read every file on every poll) and deliberately not
    git-aware (it must also catch an editor save or a stash pop, and the checkout may
    not be a git repo at all)."""
    h = hashlib.sha256()
    try:
        for f in sorted(path.rglob("*.py")):
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(path)}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    except OSError:
        return ""
    return h.hexdigest()


def _record_source_fingerprint(manifest: PluginManifest) -> None:
    """Stamp a symlinked plugin's sources at the moment its modules were imported."""
    try:
        if not manifest.path.is_symlink():
            return
        fp = _source_fingerprint(manifest.path)
        if fp:
            _SOURCE_FINGERPRINTS[manifest.id] = (str(manifest.path), fp)
    except OSError:  # pragma: no cover — a vanished path is not worth failing a load over
        pass


def code_drift_warning() -> str | None:
    """Warn when a symlinked plugin's sources changed since its modules were imported.

    Recomputed live (like the co-location and fleet-skew banners) so it appears when a
    checkout moves and clears the moment the plugin is reloaded. Returns ``None`` when
    nothing is symlinked — the common case, and one ``is_symlink()`` per tracked plugin."""
    drifted = []
    for pid, (path, fp) in sorted(_SOURCE_FINGERPRINTS.items()):
        if _source_fingerprint(Path(path)) != fp:
            drifted.append(pid)
    if not drifted:
        return None
    names = ", ".join(drifted)
    return (
        f"[plugins] source changed on disk since import: {names}. This plugin is symlinked to a "
        "live checkout, and Python caches imported modules — so the process is now serving a MIX "
        "of the imported code and whatever is on disk now (anything imported lazily after the "
        "change loads the new version). Reload the plugin or restart the agent; until then, "
        "treat its behaviour as undefined."
    )


def run_plugin_mcp_main(plugin_id: str) -> None:
    """Frozen-binary entrypoint for a plugin's managed MCP server (ADR 0019).

    Find the plugin by id across the default roots, import its entry module, and
    call its ``mcp_main()`` (the subprocess body of its managed MCP server). Used
    by the ``--mcp-plugin <id>`` shim when there's no ``python`` on PATH. Importing
    the module does NOT call ``register`` — only defines its functions — so this
    is side-effect-free apart from running the server.
    """
    from infra.paths import instance_paths

    ip = instance_paths()
    roots = [ip.app_root / "plugins", ip.plugins_dir]
    for manifest in discover_plugins(roots):
        if manifest.id != plugin_id:
            continue
        entry = _entry_file(manifest)
        if entry is None:
            raise RuntimeError(f"plugin {plugin_id!r} has no entry module")
        module = _load_plugin_module(manifest, entry)
        mcp_main = getattr(module, "mcp_main", None)
        if not callable(mcp_main):
            raise RuntimeError(f"plugin {plugin_id!r} has no mcp_main()")
        mcp_main()
        return
    raise RuntimeError(f"plugin {plugin_id!r} not found for --mcp-plugin")


def _host_version() -> str:
    """The running protoAgent version, for the manifest compat gate.

    Delegates to the shared resolver ``infra.paths.package_version()`` — the same
    source the A2A card advertises (``server.a2a._package_version``; ``graph``
    must not import ``server``, so both delegate to the ``infra`` leaf) — so the
    gate and the card can never disagree. The resolver prefers the repo
    ``pyproject.toml`` over installed metadata: on a dev checkout the editable
    install's dist-info goes stale on version bumps, and this gate used to refuse
    valid plugins because of it (#1644).
    """
    from infra.paths import package_version

    return package_version()


def _min_version_gate(manifest: PluginManifest) -> str | None:
    """Enforce the manifest's ``min_protoagent_version`` compat guard.

    Returns ``None`` when the plugin may load, or a refusal reason when the
    plugin declares it needs a *newer* host than this one — a plugin written
    against a newer plugin SDK can break the host, so refusing matches the
    manifest's documented "warn/refuse on an older host" promise. A malformed
    version string (either side) only warns and loads — a typo in a manifest
    must not brick the plugin.
    """
    declared = (manifest.min_protoagent_version or "").strip()
    if not declared:
        return None
    from packaging.version import InvalidVersion, Version

    host_raw = _host_version()
    try:
        needed, host = Version(declared), Version(host_raw)
    except InvalidVersion:
        log.warning(
            "[plugins] %s: unparseable min_protoagent_version %r (host %r) — loading anyway",
            manifest.id,
            declared,
            host_raw,
        )
        return None
    if needed > host:
        return f"requires protoAgent >= {declared} but this host is {host_raw}"
    return None


def _served_paths(routers: list[dict]) -> set[str]:
    """The set of URL paths served by *routers* (``[{"router", "prefix"}, …]``).

    Each path is the router's ``prefix`` + a contained route's ``path``. A route
    at the prefix root (``route.path`` of ``""`` or ``"/"``) is normalised to the
    bare prefix so a view declared as the prefix itself counts as served.
    """
    served: set[str] = set()
    for r in routers:
        prefix = str(r.get("prefix", "")).rstrip("/")
        router = r.get("router")
        for route in getattr(router, "routes", []) or []:
            rp = getattr(route, "path", None)
            if rp is None:
                continue
            full = (prefix + str(rp)).rstrip("/") or "/"
            served.add(full)
    return served


def _warn_unserved_views(manifest: PluginManifest, routers: list[dict]) -> None:
    """Warn for each enabled iframe page whose ``path`` no router serves.

    Rail views (ADR 0026) and path-backed Configure tabs (#3180) share the same
    sandbox host. If no registered router serves a declared page, the iframe renders
    blank/404 — usually a missing ``register_router`` or a path typo. Query strings
    and fragments select state inside a served page and are not part of its route.
    """
    pages = [
        ("view", view.get("id"), view.get("path"))
        for view in manifest.views
    ]
    pages.extend(
        ("Configure tab", tab.get("id"), tab.get("path"))
        for tab in getattr(manifest, "settings_tabs", [])
        if tab.get("path")
    )
    if not pages:
        return
    served = _served_paths(routers)
    for kind, page_id, declared in pages:
        path = _iframe_page_route(declared).rstrip("/") or "/"
        if path not in served:
            log.warning(
                "[plugins] %s: %s %r declares path %r but no registered router serves it "
                "— it will render a blank/404 iframe (did you forget register_router, "
                "or is the path a typo?)",
                manifest.id,
                kind,
                page_id,
                declared,
            )


def _warn_unserved_commands(manifest: PluginManifest, routers: list[dict]) -> None:
    """Warn for each palette command (ADR 0057) whose API route no router serves.

    The view check above catches a blank iframe; this catches its palette equivalent —
    a command row that 404s the instant an operator picks it, which reads as a broken
    feature rather than a missing ``register_router``. ``_parse_commands`` has already
    confined every route to this plugin's ``/api/plugins/<id>/`` namespace, so the only
    question left is whether the plugin registered the router that answers it.

    Exact-match, like the view check: a declarative command route carries no arguments,
    so it is a fixed path, and a router that serves it only through a ``{param}``
    placeholder is rare enough to be worth a false warning over a missed one.
    """
    routes: list[tuple[str, str]] = []
    for command in manifest.commands:
        action = command.get("action") or {}
        if action.get("type") == "tool":
            routes.append((command.get("id", "?"), action.get("route", "")))
        provider = command.get("provider") or {}
        if provider.get("route"):
            routes.append((command.get("id", "?"), provider["route"]))
    if not routes:
        return
    served = _served_paths(routers)
    for command_id, route in routes:
        full = f"/api/plugins/{manifest.id}/{route}"
        if full not in served:
            log.warning(
                "[plugins] %s: command %r declares route %r but no registered router serves "
                "%s — the palette entry will 404 when it is picked (did you forget "
                "register_router, or is the route a typo?)",
                manifest.id,
                command_id,
                route,
                full,
            )


def _sweep_plugin_jobs(plugin_id: str) -> None:
    """Cancel a not-enabled plugin's ``plugin:<id>:*`` scheduler jobs (#1642) —
    best-effort, never breaks a load. See ``sdk.cancel_plugin_jobs``."""
    try:
        from graph.sdk import cancel_plugin_jobs

        cancelled = cancel_plugin_jobs(plugin_id)
    except Exception:  # noqa: BLE001 — hygiene must never break plugin loading
        return
    if cancelled:
        log.info("[plugins] %s is disabled — cancelled %d scheduled job(s) it owned", plugin_id, cancelled)


# ── Required-config / incomplete-plugin gate (#1719) ─────────────────────────
# A plugin marks a setting `required: true` (a `settings[]` field spec) to say "I
# need this to function". If the resolved config leaves a required field blank, the
# plugin still LOADS but is flagged `incomplete`, and its tools are swapped for a
# same-signature stand-in that returns a friendly "needs setup" notice instead of
# erroring cryptically. Enable/disable stays whole-plugin; this is a soft gate.


def _is_blank(value: object) -> bool:
    """A config value that counts as 'not provided' — ``None``, an empty/whitespace
    string, or an empty collection. ``0``/``False`` are real values, not blank."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


# The setup-gap key the loader reports unsatisfied declared pip deps under (#3450).
DEPS_GAP_KEY = "deps-missing"
_MAX_NAMED_DEPS = 6


def _named(names: list[str]) -> str:
    head = ", ".join(sorted(names)[:_MAX_NAMED_DEPS])
    extra = len(names) - _MAX_NAMED_DEPS
    return f"{head} (+{extra} more)" if extra > 0 else head


def _deps_gap_message(plugin_id: str, hard: list[str], soft: list[str]) -> str | None:
    """The operator-facing line for an enabled plugin whose declared pip deps are absent,
    or ``None`` when nothing is missing. Reads as a continuation of the banner's
    ``"<Plugin>: "`` prefix, like every other gap message."""
    if not hard and not soft:
        return None
    parts = []
    if hard:
        parts.append(f"required: {_named(hard)}")
    if soft:
        parts.append(f"optional: {_named(soft)}")
    lead = (
        "needs Python packages that aren't installed"
        if hard
        else "is missing optional Python packages, so parts of it don't work"
    )
    return (
        f"{lead} — {'; '.join(parts)}. Install them in Settings ▸ Plugins or with "
        f"`protoagent plugin install-deps {plugin_id}`."
    )


def _report_deps_gap(manifest: PluginManifest) -> list[str]:
    """Report (or clear) the "declared pip deps aren't installed" gap for an ENABLED
    plugin, and return the missing dist names.

    Nothing else covers this. ``install`` deliberately doesn't install deps (ADR 0027
    D4); the ``ModuleNotFoundError`` branch below only fires for a plugin that imports
    them at MODULE level; and ``/api/plugins/installed`` — what the console's deps report
    and the wizard's post-install step read — enumerates the live plugins dir, so a
    BUNDLED plugin has no row there to carry a ``deps_missing`` badge at all.

    cowork (#3450) is both: bundled, and its document skills import the libraries inside
    ``execute_code`` rather than in-process. On a fresh server a Cowork-archetype first
    run therefore completed with the plugin enabled and four of five document libraries
    absent, with no warning on any surface — the first symptom was an ImportError from
    inside a code run, and the skill text then sent the operator to a console button that
    had nothing to render. Both tiers are reported for the same reason: cowork's whole
    stack is the OPTIONAL tier (#1954), so a hard-tier-only check would still say nothing.
    """
    from graph.plugins import installer
    from graph.plugins import setup_gaps

    hard, soft = list(manifest.requires_pip or []), list(manifest.optional_pip or [])
    if not hard and not soft:
        setup_gaps.report(manifest.id, DEPS_GAP_KEY, None)
        return []
    scopes = getattr(manifest, "pip_scopes", {}) or {}
    hard_missing = installer._deps_satisfied(hard, scopes)[1] if hard else []
    soft_missing = installer._deps_satisfied(soft, scopes)[1] if soft else []
    message = _deps_gap_message(manifest.id, hard_missing, soft_missing)
    if message:
        log.warning(
            "[plugins] %s enabled but declared deps are missing (%s) — run: protoagent plugin install-deps %s",
            manifest.id,
            ", ".join(sorted([*hard_missing, *soft_missing])),
            manifest.id,
        )
    setup_gaps.report(
        manifest.id,
        DEPS_GAP_KEY,
        message,
        label=str(manifest.name or manifest.id),
        # The one fix, as closed data: the plugin's own Settings section, where
        # "Install deps" lives. Never a URL or a callback (setup_gaps.ACTION_KINDS).
        action={"kind": "plugin_config"},
    )
    return sorted([*hard_missing, *soft_missing])


def _missing_required_config(manifest: PluginManifest, resolved: dict) -> list[dict]:
    """Required settings (``settings[].required``) left blank in the resolved config.
    Returns ``[{key, label}]`` — empty ⇒ the plugin has everything it declared it needs.
    Secrets resolve into ``resolved`` too (unset ⇒ ``""``), so this covers API keys."""
    out: list[dict] = []
    for spec in manifest.settings:
        if not (isinstance(spec, dict) and spec.get("required")):
            continue
        key = str(spec.get("key") or "").strip()
        if not key:
            continue
        if _is_blank(resolved.get(key)):
            out.append({"key": key, "label": str(spec.get("label") or key)})
    return out


def _needs_config_tool(orig, plugin_name: str, needs: list[dict]):
    """Same-signature stand-in for a tool whose plugin is missing required config —
    the model sees the same name/description/args but the call returns a friendly
    'needs setup' notice, so the agent can point the operator at configuration instead
    of surfacing a cryptic KeyError/None-credential failure (#1719)."""
    from langchain_core.tools import StructuredTool

    fields = ", ".join(n["label"] for n in needs) or "required config"
    msg = (
        f"⚠️ The {plugin_name} plugin needs setup before this tool works — "
        f"missing: {fields}. Ask the operator to complete it in the plugin's settings."
    )

    def _blocked(*_args, **_kwargs) -> str:
        return msg

    async def _ablocked(*_args, **_kwargs) -> str:
        return msg

    return StructuredTool(
        name=orig.name,
        description=orig.description,
        args_schema=orig.args_schema,
        func=_blocked,
        coroutine=_ablocked,
    )


def load_plugins(config, *, core_tool_names: set[str] | None = None) -> PluginLoadResult:
    """Load enabled plugins and collect their contributions.

    ``core_tool_names`` lets the caller pass the already-registered tool names so
    plugin tools that would shadow them are skipped (the OpenClaw collision rule).
    """
    result = PluginLoadResult()
    _prepend_plugin_deps_to_syspath()
    roots = _plugin_roots(config)
    enabled_ids = set(getattr(config, "plugins_enabled", []) or [])
    disabled_ids = set(getattr(config, "plugins_disabled", []) or [])
    seen_tool_names = set(core_tool_names or set())
    superseded: dict[str, dict] = {}

    for manifest in discover_plugins(roots, superseded=superseded):
        # A builtin (core runtime infrastructure, e.g. the delegate registry) always
        # loads — it ignores the enable gate AND the disabled list, so it can't be
        # turned off. Otherwise plugins.disabled wins: turn off a bundled plugin (e.g.
        # a first-party surface) without deleting it or editing core.
        enabled = manifest.builtin or (
            (manifest.enabled or manifest.id in enabled_ids) and manifest.id not in disabled_ids
        )
        entry = {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "enabled": enabled,
            # Built-in plugins are filtered out of the Plugins management list (they
            # aren't optional add-ons) — the flag rides along in /api/runtime/status.
            "builtin": manifest.builtin,
            "loaded": False,
            # Required-config gate (#1719) — set True + populated below when an enabled
            # plugin loads but a `required: true` setting is blank. Present on every
            # entry so consumers can rely on the shape.
            "incomplete": False,
            "needs_config": [],
            # Declared pip deps that aren't installed anywhere this plugin can import
            # them (#3450) — populated for a plugin that LOADS; `[]` on every other
            # entry so consumers can rely on the shape.
            "deps_missing": [],
            "tools": [],
            "skills": 0,
            # Console surfaces (ADR 0026) — the rail reads these from /api/runtime/status. Views
            # are sandboxed iframes (ADR 0038); the plugin serves its own page at `path`.
            "views": list(manifest.views) if enabled else [],
            # Declarative command-palette entries (ADR 0057). Enable-gated like every
            # other runnable contribution: a palette row is a dispatch, and an installed
            # -but-not-enabled plugin must not be able to fire one (install != enable !=
            # trust). NOT to be confused with `chat_commands` below, which counts the
            # register_chat_command tokens a loaded plugin registered in Python.
            "commands": list(manifest.commands) if enabled else [],
            # Ordered per-plugin Configure tabs (#3179/#3180). Schema-backed tabs
            # carry id/label; path-backed tabs add a sandboxed plugin-owned page.
            # Disabled plugins expose no runnable page contribution.
            "settings_tabs": list(manifest.settings_tabs) if enabled else [],
            # Event contract (ADR 0039) — declared topics this plugin emits / subscribes to,
            # surfaced for discoverability (the console can show a plugin's event catalog).
            "emits": list(manifest.emits) if enabled else [],
            "subscribes": list(manifest.subscribes) if enabled else [],
            # Typed event contracts (#1636) — topic → {summary?, schema?} for emits
            # entries that declared a payload shape. Rides /api/runtime/status so a
            # cross-plugin consumer can discover the contract instead of reverse-
            # engineering the emitter. Declarative only — no publish-time validation.
            "emits_schemas": dict(manifest.emits_schemas) if enabled else {},
        }

        if not enabled:
            # Lifecycle hygiene (#1642): a disabled plugin must not keep a recurring
            # cadence firing — sweep its `plugin:<id>:*` scheduler jobs on every
            # (re)load. The console disable toggle and a hand-edited config both
            # funnel through a (re)load, so this one hook covers both; uninstall is
            # covered by the installer (the manifest is gone from disk, so this loop
            # can't see it). Pre-setup loads run before the scheduler is wired
            # (STATE.scheduler is None → no-op).
            _sweep_plugin_jobs(manifest.id)
            # A disabled plugin's setup-gap banners must not outlive it (setup_gaps seam).
            from graph.plugins import setup_gaps as _setup_gaps

            _setup_gaps.clear_plugin(manifest.id)
            result.meta.append(entry)
            continue

        # A retired git-installed copy of a plugin that now ships with protoAgent
        # (``supersedes``): the bundled copy is what loads, and the operator is told the
        # leftover can go. Reported (or cleared) on every load, so the banner leaves the
        # moment the copy does — whoever removed it.
        note = superseded.get(manifest.id)
        if note:
            log.warning(
                "[plugins] %s: loading the bundled copy (v%s) — the installed copy at %s (v%s, from %s) "
                "is superseded and ignored; uninstall it to clean up",
                manifest.id,
                note["bundled_version"],
                note["installed_path"],
                note["installed_version"],
                display_source(note["source_url"]),
            )
            if _version_key(note["installed_version"]) >= _version_key(note["bundled_version"]):
                # The move's contract: the bundled copy is NEWER than every standalone
                # release. If it isn't, and this copy ever loses its lock row (a hand
                # edit, a reset lock), the #1574 rule lets it — untracked and not older —
                # shadow the bundled copy again. Say so while it's still recorded.
                log.warning(
                    "[plugins] %s: the superseded installed copy (v%s) is not older than the bundled one "
                    "(v%s) — a bundled plugin must be versioned above every release of the repo it "
                    "supersedes; remove the installed copy",
                    manifest.id,
                    note["installed_version"],
                    note["bundled_version"],
                )
        from graph.plugins import setup_gaps as _setup_gaps

        _setup_gaps.report(
            manifest.id,
            SUPERSEDED_GAP_KEY,
            _superseded_message(manifest.id, note) if note else None,
            label=str(manifest.name or manifest.id),
        )

        missing = [v for v in manifest.requires_env if not os.environ.get(v)]
        if missing:
            entry["error"] = f"missing env: {', '.join(missing)}"
            log.warning("[plugins] %s enabled but %s — skipping", manifest.id, entry["error"])
            result.meta.append(entry)
            continue

        # min_protoagent_version compat gate (ADR 0027) — refuse before the
        # plugin's code ever imports (a plugin built for a newer SDK can break
        # the host); malformed versions warn inside the gate and load anyway.
        incompat = _min_version_gate(manifest)
        if incompat:
            entry["error"] = incompat
            log.error("[plugins] %s enabled but %s — refusing to load", manifest.id, incompat)
            result.meta.append(entry)
            continue

        # Lifecycle timing (#2675): each stage per plugin — load (module import),
        # config (resolved-config binding), registration (register(registry)). The
        # helper records in `finally`, so a stage that raises into the except below
        # still gets timed — that's the plugin worth diagnosing.
        try:
            with timed_lifecycle_phase(manifest.id, "load"):
                register = _import_register(manifest)
            with timed_lifecycle_phase(manifest.id, "config"):
                # Resolved config section (ADR 0019) — defaults if not in plugin_config.
                section = manifest.config_section or manifest.id
                pconf = (getattr(config, "plugin_config", {}) or {}).get(section) or dict(manifest.config or {})
                registry = PluginRegistry(manifest.id, manifest.path, config=pconf, config_section=section)
                registry.display_name = str(manifest.name or manifest.id)
            with timed_lifecycle_phase(manifest.id, "registration"):
                register(registry)
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not break boot
            # Clear diagnostic when an enabled plugin's declared deps aren't
            # installed (ADR 0027 D4: install fetches code; deps are explicit).
            if isinstance(exc, ModuleNotFoundError) and (manifest.requires_pip or manifest.optional_pip):
                entry["error"] = (
                    f"declared deps not installed ({', '.join([*manifest.requires_pip, *manifest.optional_pip])}) — "
                    f"run: python -m server plugin install-deps {manifest.id}"
                )
            else:
                entry["error"] = str(exc)
                # The agent iterates on this (ADR 0096 D4): ``str(exc)`` alone gives a
                # NameError with no location. Bounded — the meta rides /api/runtime/status.
                entry["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]
            log.warning("[plugins] %s failed to load: %s — skipping", manifest.id, entry["error"])
            result.meta.append(entry)
            continue

        # Required-config gate (#1719) — the plugin loaded, but if it declared
        # required settings that are still blank, flag it incomplete and swap its
        # tools for needs-setup stand-ins (below) so a misconfigured plugin degrades
        # gracefully instead of erroring mid-call.
        needs_config = _missing_required_config(manifest, pconf)
        if needs_config:
            entry["incomplete"] = True
            entry["needs_config"] = needs_config
            log.warning(
                "[plugins] %s loaded but missing required config (%s) — its tools return a needs-setup notice",
                manifest.id,
                ", ".join(n["key"] for n in needs_config),
            )

        # Declared-deps gate (#3450) — the plugin loaded, but its `requires_pip` /
        # `optional_pip` may not be installed anywhere it can import them. Reported as a
        # setup gap (and cleared the same way) on every (re)load, so `install-deps` +
        # reload self-heals the banner. See `_report_deps_gap` for why nothing else sees it.
        entry["deps_missing"] = _report_deps_gap(manifest)

        kept = []
        for tool in registry.tools:
            if tool.name in seen_tool_names:
                log.warning("[plugins] %s: tool %s collides with an existing tool — skipped", manifest.id, tool.name)
                continue
            seen_tool_names.add(tool.name)
            kept.append(_needs_config_tool(tool, manifest.name or manifest.id, needs_config) if needs_config else tool)

        result.tools.extend(kept)
        # Attribute each kept tool to this plugin (display name, id fallback) for the Tools tab.
        for tool in kept:
            result.tool_plugins[tool.name] = manifest.name or manifest.id
        result.skill_dirs.extend(registry.skill_dirs)
        result.workflow_dirs.extend(registry.workflow_dirs)
        # Full-bundle auto-discovery (ADR 0027): a plugin repo can ship SKILL.md
        # skills + *.yaml workflows in conventional subdirs and they're picked up
        # without any register_* boilerplate — so installing a repo pulls in the
        # whole bundle (tools+subagents via register(), skills+workflows as data).
        conv_skills = manifest.path / "skills"
        if conv_skills.is_dir() and conv_skills not in result.skill_dirs:
            result.skill_dirs.append(conv_skills)
        conv_workflows = manifest.path / "workflows"
        if conv_workflows.is_dir() and conv_workflows not in result.workflow_dirs:
            result.workflow_dirs.append(conv_workflows)
        # A2A card skills — attributed to their plugin and deduped across plugins
        # (first registration wins, like tools), so a cross-plugin id collision is
        # rejected visibly at load instead of shipping a duplicate card entry that
        # the finalizer silently resolves first-wins (#2754).
        for spec in registry.a2a_skills:
            owner = result.a2a_skill_plugins.get(spec["id"])
            if owner is not None:
                log.warning(
                    "[plugins] %s: a2a skill %r collides with one from %s — skipped",
                    manifest.id,
                    spec["id"],
                    owner,
                )
                continue
            result.a2a_skills.append(spec)
            result.a2a_skill_plugins[spec["id"]] = manifest.id
        if registry.thread_id_resolver is not None:  # last plugin wins (#571)
            if result.thread_id_resolver is not None:
                log.warning("[plugins] %s overrides a thread_id resolver already set by another plugin", manifest.id)
            result.thread_id_resolver = registry.thread_id_resolver
        # Surfaces / routes / subagents (ADR 0018) — tagged with the plugin id so
        # the server can namespace + report them.
        for r in registry.routers:
            result.routers.append({"plugin_id": manifest.id, **r})
        # Manifest-declared auth-exempt prefixes (already namespace-scoped by the
        # parser) — the server hands these to the auth middleware so an inbound
        # webhook / public view page works under a token gate.
        result.public_paths.extend(manifest.public_paths)
        # Federation-tier prefixes (#2747) — same namespace scoping; the auth middleware
        # lowers the /api operator ceiling to the federation tier on these, nothing more.
        result.federation_paths.extend(manifest.federation_paths)
        # Cross-check: every declared view must be served by one of this plugin's
        # routers, else the iframe renders blank/404. Catches "declared a view but
        # forgot register_router" / a path typo that fails silently today.
        _warn_unserved_views(manifest, registry.routers)
        _warn_unserved_commands(manifest, registry.routers)
        for s in registry.surfaces:
            result.surfaces.append({"plugin_id": manifest.id, **s})
        result.subagents.extend(registry.subagents)
        result.middleware.extend(registry.middleware)  # ADR 0032
        result.late_tool_factories.extend(registry.late_tool_factories)  # late-tools seam
        for name, fn in registry.goal_verifiers.items():  # ADR 0028
            if name in result.goal_verifiers:
                log.warning("[plugins] %s: goal verifier %s collides — skipped", manifest.id, name)
                continue
            result.goal_verifiers[name] = fn
            # Carry the describe-me half alongside (used by GET /api/verifiers); the
            # collision guard above already decided this name is ours.
            meta = registry.goal_verifier_meta.get(name)
            if meta:
                result.goal_verifier_meta[name] = meta
        for name, fn in registry.work_providers.items():  # ADR 0079 (Observe)
            if name in result.work_providers:
                log.warning("[plugins] %s: work provider %s collides — skipped", manifest.id, name)
                continue
            result.work_providers[name] = fn
            meta = registry.work_provider_meta.get(name)
            if meta:
                result.work_provider_meta[name] = meta
        result.goal_hooks.extend(registry.goal_hooks)  # ADR 0028 D4
        result.watch_hooks.extend(registry.watch_hooks)  # ADR 0067
        result.lifecycle_hooks.extend(registry.lifecycle_hooks)  # ADR 0074
        for name, factory in registry.knowledge_stores.items():  # ADR 0031
            if name in result.knowledge_stores:
                log.warning("[plugins] %s: knowledge backend %s collides — skipped", manifest.id, name)
                continue
            result.knowledge_stores[name] = factory
        for name, factory in registry.embedders.items():  # ADR 0031 follow-up
            if name in result.embedders:
                log.warning("[plugins] %s: embedder %s collides — skipped", manifest.id, name)
                continue
            result.embedders[name] = factory
        for f in registry.mcp_servers:
            result.mcp_servers.append({"plugin_id": manifest.id, "factory": f})
        for token, handler in registry.chat_commands.items():  # user-only chat control commands
            if token in result.chat_commands:
                log.warning("[plugins] %s: chat command /%s collides — skipped", manifest.id, token)
                continue
            result.chat_commands[token] = handler
        entry["loaded"] = True
        entry["tools"] = [t.name for t in kept]
        # Count the conventional skills/ dir too (auto-discovered above) — counting only
        # explicit register_skill_dir calls made every convention-shipped skill read as
        # "0 skill dir(s)" in the meta + boot log while actually loading fine.
        entry["skills"] = len(registry.skill_dirs) + (1 if (manifest.path / "skills").is_dir() else 0)
        entry["routers"] = len(registry.routers)
        entry["surfaces"] = len(registry.surfaces)
        entry["subagents"] = [getattr(c, "name", "?") for c in registry.subagents]
        entry["mcp_servers"] = len(registry.mcp_servers)
        entry["chat_commands"] = [f"/{t}" for t in registry.chat_commands]
        result.meta.append(entry)
        log.info(
            "[plugins] loaded %s: %d tool(s), %d skill dir(s), %d route(s), "
            "%d surface(s), %d subagent(s), %d middleware, %d mcp server(s), %d chat command(s)",
            manifest.id,
            len(kept),
            entry["skills"],
            len(registry.routers),
            len(registry.surfaces),
            len(registry.subagents),
            len(registry.middleware),
            len(registry.mcp_servers),
            len(registry.chat_commands),
        )

    # Setup gaps (setup_gaps seam) from plugins that are no longer on disk at all must
    # not outlive them — the disabled-branch clear above never visits an uninstalled id.
    try:
        from graph.plugins import setup_gaps as _setup_gaps

        _setup_gaps.retain({str(m.get("id")) for m in result.meta})
    except Exception:  # noqa: BLE001 — hygiene must never break plugin loading
        pass
    return result


def _plugin_roots(config) -> list[Path]:
    from infra.paths import instance_paths

    ip = instance_paths()
    live_override = getattr(config, "plugins_dir", "") or ""
    live_root = Path(live_override).expanduser() if live_override else ip.plugins_dir
    return [ip.app_root / "plugins", live_root]  # bundle first, live overrides


def _prepend_plugin_deps_to_syspath() -> None:
    """Put each provisioned per-plugin wheel-deps dir (ADR 0093) on ``sys.path`` before
    any plugin imports, so a plugin whose ``requires_pip`` was installed as unbundled
    wheels can import them in the frozen app — the same "writable dir on sys.path"
    mechanism that already makes the live plugins root work when frozen. No-op (and
    dependency-free) when nothing's been installed. Best-effort: never break plugin
    loading over a deps-dir read."""
    try:
        from graph.plugins.wheel_installer import existing_deps_dirs, prepend_to_syspath

        for d in existing_deps_dirs():
            prepend_to_syspath(d)
    except Exception:  # noqa: BLE001 — deps discovery must never break the loader
        log.warning("[plugins] failed to add wheel-deps dirs to sys.path", exc_info=True)
