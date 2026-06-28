"""Plugin manifest (``protoagent.plugin.yaml``) parsing."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("protoagent.plugins")

MANIFEST_FILENAME = "protoagent.plugin.yaml"


@dataclass
class PluginManifest:
    """Declared metadata for a plugin. ``id`` + ``name`` are required."""

    id: str
    name: str
    path: Path
    version: str = "0.0.0"
    description: str = ""
    # ``enabled: true`` in the manifest is an author opt-in (for plugins you
    # wrote/dropped in yourself). An operator can also enable by id via
    # ``plugins.enabled`` in config. Either path counts as consent.
    enabled: bool = False
    # ``builtin: true`` marks a plugin as core runtime infrastructure (e.g. the
    # delegate registry): it ALWAYS loads — ignoring both the enable gate and the
    # ``plugins.disabled`` list — and is hidden from the Plugins management list,
    # since it isn't an optional add-on the operator toggles. Its config lives in
    # the core Workspace settings, not the Plugins panel.
    builtin: bool = False
    requires_env: list[str] = field(default_factory=list)
    # Declarative, for transparency in the console — not yet enforced.
    capabilities: dict = field(default_factory=dict)
    entrypoint: str = ""  # optional module filename; defaults to __init__.py / plugin.py
    # Plugin config (ADR 0019) — declared as data so it's known at config-load /
    # secret-strip / settings-schema time, before register() ever imports.
    #   config_section: the top-level YAML section the plugin claims (default: id)
    #   config:    defaults for that section (key → default value)
    #   secrets:   keys in the section routed to the secrets.yaml overlay
    #   settings:  Settings-schema field specs ({key, label, type, ...})
    config_section: str = ""
    config: dict = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    # Test action (ADR 0029) — when true, the plugin serves a credential check at
    # `POST /api/config/test-<config_section>` (e.g. the chat_surface wirer mounts
    # one), and the console renders a generic "Test connection" button for the
    # group. No console edit needed per plugin.
    test: bool = False
    # Optional setup-guide URL (ADR 0059) — the console renders a generic "Setup
    # guide" link next to the plugin's settings, so no per-plugin frontend is needed.
    guide_url: str = ""
    # Console surfaces (ADR 0026) — each entry adds a left-rail icon opening a
    # full view (an iframe of a page the plugin serves at `path`). Declared as
    # data so it's known without importing the plugin, and surfaced to the
    # frontend via /api/runtime/status. Each: {id, label, icon, path, tabs?, slot?}.
    # `path` must (1) be a path a registered router actually serves — the console
    # iframes it verbatim, so a path no router answers is a blank surface — and
    # (2) be a same-origin RELATIVE path (no scheme/host/port): an absolute URL
    # escapes the ADR 0042 fleet proxy origin and breaks the same-origin
    # postMessage token handshake. See `_parse_views` (warns on non-same-origin)
    # and docs/guides/building-react-plugin-views.md.
    views: list[dict] = field(default_factory=list)
    # Auth-exempt paths — prefixes under THIS plugin's own /plugins/<id>/ (or
    # /api/plugins/<id>/) namespace that the default-deny auth middleware lets
    # through WITHOUT a bearer. The escape hatch for an inbound webhook (no bearer
    # — the plugin verifies its own signature) or a public view page that must load
    # in a browser iframe under a token-gated deployment. Namespace-scoped by the
    # parser so a plugin can never exempt a core route.
    public_paths: list[str] = field(default_factory=list)
    # Event contract (ADR 0039) — the topics this plugin broadcasts / listens for.
    # Declarative for discoverability (surfaced in /api/runtime/status): a plugin
    # "ships" its events as its public API so others subscribe by topic without
    # importing it. Not enforced — publish is auto-namespaced + guarded at runtime;
    # subscribing to any topic is allowed.
    emits: list[str] = field(default_factory=list)
    subscribes: list[str] = field(default_factory=list)
    # Distribution (ADR 0027) — for plugins installed from a git URL.
    #   requires_pip: declared pip deps. NOT auto-installed (install ≠ code exec);
    #     the operator installs them explicitly. Missing → clear error on enable.
    #   repository/homepage: provenance, shown in the install review.
    #   min_protoagent_version: compat guard — the loader refuses to load the
    #     plugin when the host is older than declared (malformed strings only
    #     warn and load).
    requires_pip: list[str] = field(default_factory=list)
    repository: str = ""
    homepage: str = ""
    min_protoagent_version: str = ""


# A view path that carries a scheme/host instead of being a same-origin relative
# path. Console views are sandboxed iframes served back through the ADR 0042 fleet
# proxy and rely on a same-origin postMessage token handshake — an absolute URL
# (http(s)://…, protocol-relative //host, localhost, or an explicit :PORT) escapes
# the proxy origin and breaks both. We warn (not reject) so a typo is loud but the
# plugin still loads.
_NON_SAME_ORIGIN_PATH = re.compile(r"https?://|^//|localhost|:\d", re.IGNORECASE)


def _parse_views(views, plugin_id: str) -> list[dict]:
    """Keep view entries with an ``id`` + ``path``; warn on non-same-origin paths.

    Views must point at a same-origin **relative** path. A path that carries a
    scheme or host (``http(s)://``, protocol-relative ``//host``, ``localhost``, or
    a ``:PORT``) breaks the ADR 0042 fleet proxy and the same-origin postMessage
    token handshake — we log a warning but still keep the view so the author sees
    the mistake rather than a silently-missing rail icon.
    """
    if not isinstance(views, (list, tuple)):
        return []
    kept: list[dict] = []
    for v in views:
        if not (isinstance(v, dict) and v.get("id") and v.get("path")):
            continue
        path = str(v.get("path"))
        if _NON_SAME_ORIGIN_PATH.search(path):
            log.warning(
                "[plugins] %s: view %r path %r is not same-origin relative — a scheme/host "
                "breaks the fleet proxy + the postMessage token handshake; use a relative path",
                plugin_id,
                v.get("id"),
                path,
            )
        kept.append(v)
    return kept


# A plugin id namespaces its routes (``/plugins/<id>/``, ``/api/plugins/<id>/``)
# and its config section, so it must be a safe slug AND must not shadow a core
# ``/api/plugins/<verb>`` management route — otherwise its ``public_paths`` could
# prefix-match and exempt that core route (e.g. install = RCE) from the auth gate.
_VALID_PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_RESERVED_PLUGIN_IDS = frozenset({"install", "installed", "sync", "updates", "catalog", "enabled"})


def _parse_public_paths(paths, plugin_id: str) -> list[str]:
    """Keep auth-exempt paths that live under THIS plugin's namespace SUBTREE
    (``/plugins/<id>/…`` or ``/api/plugins/<id>/…``); drop + warn on anything else.

    Namespace-scoping is the security boundary: a plugin can exempt only its own
    routes from the auth gate, never a core path like ``/api/config`` or the core
    ``/api/plugins/<verb>`` routes. The trailing slash is load-bearing — without
    it, id ``install`` would prefix-match the core ``/api/plugins/install`` route."""
    if not isinstance(paths, (list, tuple)):
        return []
    roots = (f"/plugins/{plugin_id}/", f"/api/plugins/{plugin_id}/")
    kept: list[str] = []
    for p in paths:
        s = str(p).strip()
        if s.startswith(roots):
            kept.append(s)
        elif s:
            log.warning(
                "[plugins] %s: public_path %r is outside the plugin namespace "
                "(/plugins/%s/… or /api/plugins/%s/…) — ignored",
                plugin_id, s, plugin_id, plugin_id,
            )
    return kept


def _view_public_paths(views: list[dict]) -> list[str]:
    """The page path of every console view (and its palette morph), to auto-exempt
    from the auth gate.

    A view page is public *chrome*: the console iframes it with a plain navigation
    that cannot carry the operator bearer, so a gated page 401-blanks under a
    token-gated deployment. Its DATA stays gated under ``/api/plugins/<id>/*`` and
    is fetched from inside the loaded page with the postMessage handshake token.

    Deriving these from ``views`` means a plugin's view loads under a token gate
    automatically — authors don't have to re-declare each view path in
    ``public_paths`` (the bundled notes/docs plugins didn't, and 401-blanked).
    Query/fragment are stripped so the prefix match covers e.g.
    ``/plugins/docs/view?mode=search``. Same-origin scoping is enforced later by
    ``_parse_public_paths``.
    """
    out: list[str] = []
    for v in views:
        candidates = [v.get("path")]
        palette = v.get("palette")
        if isinstance(palette, dict):
            candidates.append(palette.get("path"))
        for c in candidates:
            p = str(c or "").split("?", 1)[0].split("#", 1)[0].strip()
            if p:
                out.append(p)
    return out


def load_manifest(plugin_dir: Path) -> PluginManifest | None:
    """Parse ``<plugin_dir>/protoagent.plugin.yaml`` → ``PluginManifest``.

    Returns ``None`` (logged) for a missing/invalid manifest or one without the
    required ``id``/``name`` — never raises, so one bad plugin can't break
    discovery.
    """
    manifest_path = plugin_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("[plugins] %s: unreadable manifest: %s", plugin_dir.name, exc)
        return None
    if not isinstance(data, dict):
        log.warning("[plugins] %s: manifest is not a mapping", plugin_dir.name)
        return None

    pid = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    if not pid or not name:
        log.warning("[plugins] %s: manifest missing required id/name — skipping", plugin_dir.name)
        return None
    if not _VALID_PLUGIN_ID.match(pid) or pid.lower() in _RESERVED_PLUGIN_IDS:
        log.warning(
            "[plugins] %s: invalid or reserved plugin id %r — must match %s and must not shadow a "
            "core /api/plugins/ route; skipping",
            plugin_dir.name, pid, _VALID_PLUGIN_ID.pattern,
        )
        return None

    req = data.get("requires_env")
    requires_env = [str(x) for x in req] if isinstance(req, (list, tuple)) else []
    caps = data.get("capabilities")

    cfg = data.get("config")
    secrets = data.get("secrets")
    settings = data.get("settings")
    views = _parse_views(data.get("views"), pid)
    # public_paths = explicitly-declared exempt paths PLUS every view's own page
    # path (view pages are public chrome — see _view_public_paths). Both run
    # through the namespace validator; dict.fromkeys dedupes while preserving order
    # (a view path a manifest also lists explicitly collapses to one).
    public_paths = list(
        dict.fromkeys(
            [
                *_parse_public_paths(data.get("public_paths"), pid),
                *_parse_public_paths(_view_public_paths(views), pid),
            ]
        )
    )
    emits = data.get("emits")
    subscribes = data.get("subscribes")
    requires_pip = data.get("requires_pip")
    return PluginManifest(
        id=pid,
        name=name,
        path=plugin_dir,
        version=str(data.get("version", "0.0.0")),
        description=str(data.get("description", "")),
        enabled=bool(data.get("enabled", False)),
        builtin=bool(data.get("builtin", False)),
        requires_env=requires_env,
        capabilities=caps if isinstance(caps, dict) else {},
        entrypoint=str(data.get("entrypoint", "")).strip(),
        config_section=str(data.get("config_section", "")).strip() or pid,
        config=cfg if isinstance(cfg, dict) else {},
        secrets=[str(s) for s in secrets] if isinstance(secrets, (list, tuple)) else [],
        settings=[s for s in settings if isinstance(s, dict)] if isinstance(settings, (list, tuple)) else [],
        test=bool(data.get("test", False)),
        guide_url=str(data.get("guide_url", "") or "").strip(),
        views=views,
        public_paths=public_paths,
        emits=[str(x) for x in emits] if isinstance(emits, (list, tuple)) else [],
        subscribes=[str(x) for x in subscribes] if isinstance(subscribes, (list, tuple)) else [],
        requires_pip=[str(x) for x in requires_pip] if isinstance(requires_pip, (list, tuple)) else [],
        repository=str(data.get("repository", "")).strip(),
        homepage=str(data.get("homepage", "")).strip(),
        min_protoagent_version=str(data.get("min_protoagent_version", "")).strip(),
    )
