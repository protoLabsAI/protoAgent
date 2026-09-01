"""Plugin manifest (``protoagent.plugin.yaml``) parsing."""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import yaml

log = logging.getLogger("protoagent.plugins")

MANIFEST_FILENAME = "protoagent.plugin.yaml"


@dataclass
class PluginManifest:
    """Declared metadata for a plugin. ``id`` + ``name`` are required."""

    # Identity. ``id`` is the slug everything else is namespaced by — routes
    # (``/plugins/<id>/``), the config section, event topics, scheduler job ids — so it
    # must be unique on the instance, match ``[A-Za-z0-9][A-Za-z0-9_-]*``, and avoid the
    # reserved management verbs (``install``, ``sync``, ``catalog``, …).
    #   id:   the plugin's slug — required, unique, namespaces everything it owns
    #   name: the human label the console shows — required
    #   path: the plugin's directory on disk. Resolved by the loader, never authored.
    id: str
    name: str
    path: Path
    # Presentation + release metadata. ``version`` is what the update/pin machinery
    # compares (ADR 0049), so bump it on every release or an installed copy looks current
    # forever.
    #   version:     semantic version string; drives update + pin resolution
    #   description: one-line summary shown in the console's plugin list
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
    # Env vars that must be set for the plugin to load — a HARD gate: a missing one skips
    # the plugin with a logged reason rather than half-loading it. Use it for what the
    # plugin cannot function without; use ``settings[].required`` (ADR 0019) instead when
    # the operator should be prompted in the console rather than blocked at boot.
    requires_env: list[str] = field(default_factory=list)
    # Declarative, for transparency in the console — not yet enforced.
    capabilities: dict = field(default_factory=dict)
    # The module filename the loader imports to find ``register(registry)``. Empty means
    # the default search: ``__init__.py``, then ``plugin.py``.
    entrypoint: str = ""
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
    # Ordered Configure-dialog tabs (#3179/#3180). A schema-backed ``{id, label}``
    # descriptor is targeted by ``settings[].tab``; a path-backed ``{id, label,
    # path}`` descriptor embeds plugin-owned UI from /plugins/<id>/... through the
    # sandboxed view bridge. One descriptor has one kind — settings cannot target a
    # path-backed tab. Plugins that omit this keep the flat Configuration form.
    settings_tabs: list[dict] = field(default_factory=list)
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
    # frontend via /api/runtime/status. Each: {id, label, icon, path, tabs?,
    # slot?, palette?}.
    # `path` must (1) be a path a registered router actually serves — the console
    # iframes it verbatim, so a path no router answers is a blank surface — and
    # (2) be a same-origin RELATIVE path (no scheme/host/port): an absolute URL
    # escapes the ADR 0042 fleet proxy origin and breaks the same-origin
    # postMessage token handshake. See `_parse_views` (warns on non-same-origin)
    # and docs/guides/building-react-plugin-views.md.
    # `palette` (ADR 0057) opts the view into the command palette's INLINE morph,
    # where picking its entry expands the view's iframe inside the palette body
    # instead of navigating to its rail panel. Exactly two spellings are honored:
    # the literal string `inline` (morph the same page `path` already renders), or
    # a mapping {path: ...} naming a DIFFERENT page to morph — a tighter
    # palette-sized editor beside the full rail panel. That mapping's page is
    # auto-exempted from the auth gate like any other view page. Every view is
    # already a "Go to …" palette entry without opting in, so `palette` is only
    # ever about the inline morph. Any other value — `true` being the tempting
    # wrong guess — is dropped with a warning and the view itself still loads,
    # because the console ignores an unrecognized shape silently.
    views: list[dict] = field(default_factory=list)
    # Palette commands (ADR 0057) — declarative command-palette entries, parsed and
    # never imported, exactly like `views`. Each: {id, title, hint?, keywords?,
    # icon?, group?, action?, provider?}, where `action` is what the entry DOES and
    # `provider` makes it a live-search row that queries the plugin for results as
    # the operator types. The console compiles both into behavior inside its own
    # trusted adapter — plugin code never enters the bundle — so the vocabulary is
    # closed and every field is validated at parse time: `navigate` and `open_view`
    # take a `view` naming a view THIS manifest declares — `navigate` opens it on its
    # rail/dock, `open_view` morphs it into the palette body and always ships
    # `inline: true` (there is no non-inline `open_view`; `navigate` is that); `tool`
    # takes a `route` under /api/plugins/<id>/ plus an optional `method` (absent ⇒
    # POST — a verb is never guessed); `emit` takes a `topic` published on the ADR
    # 0039 bus and an optional `data` mapping (the payload
    # `POST /api/events/publish` carries); `command` takes a `command` naming another
    # entry in this same list. A `provider` makes the entry a live search: it takes
    # the `route` the console queries as the operator types plus the `result_action`
    # that running one of its result rows performs — required, because the rows are
    # data and that declared action is the only thing the palette can run.
    # Unlike a bad view path (which only blanks an iframe) a bad route becomes an
    # AUTHENTICATED call carrying the operator bearer, so anything reaching outside
    # the plugin's own namespace — an absolute or cross-origin route, a ../ escape
    # (percent-encoded spellings unwrapped first), a route carrying a tab, newline or
    # other control character (the URL parser DELETES those before it resolves the dot
    # segments, so `.<TAB>./.<TAB>./config` validates clean and requests /api/config),
    # a topic in another plugin's namespace, a view or command this manifest never
    # declared — is DROPPED with a warning rather than kept. See `_parse_commands`.
    # The console's trusted adapter (apps/web/src/app/pluginPaletteCommands.ts) is what
    # turns a surviving entry into a ⌘K row — grouped with the plugin's view rows and
    # chipped with its name, in the console palette and the desktop launcher alike. It
    # MIRRORS the route and topic namespace checks above rather than trusting the status
    # payload, so an entry that fails the mirror is simply absent. Two console-side
    # limits worth knowing while writing a manifest: `open_view` needs its target view to
    # have opted into the inline morph via `views[].palette` — something this parser
    # cannot see, so a command naming a view that did not opt in opens it on its rail
    # instead of morphing — and a `provider` is parsed and shipped but not compiled yet
    # (ADR 0057 §8 leaves its per-query timeout/cancel + result-cap budget open), so an
    # entry declaring only a provider contributes no row.
    commands: list[dict] = field(default_factory=list)
    # Auth-exempt paths — prefixes under THIS plugin's own /plugins/<id>/ (or
    # /api/plugins/<id>/) namespace that the default-deny auth middleware lets
    # through WITHOUT a bearer. The escape hatch for an inbound webhook (no bearer
    # — the plugin verifies its own signature) or a public view page that must load
    # in a browser iframe under a token-gated deployment. Namespace-scoped by the
    # parser so a plugin can never exempt a core route.
    public_paths: list[str] = field(default_factory=list)
    # Federation-tier paths (#2747) — prefixes under THIS plugin's own namespace that
    # accept the *federation* credential (ADR 0066) where the ``/api`` operator ceiling
    # would otherwise 403 it. NOT auth-exempt: a valid bearer is still required; only
    # the tier ceiling is lowered. The seam for a deterministic plugin-owned RPC that a
    # peer holding only the federation token must reach (a second device syncing a
    # plugin-owned store) without being issued the operator bearer. Same
    # namespace-scoping as ``public_paths`` — a plugin can never lower a core route.
    federation_paths: list[str] = field(default_factory=list)
    # Event contract (ADR 0039) — the topics this plugin broadcasts / listens for.
    # Declarative for discoverability (surfaced in /api/runtime/status): a plugin
    # "ships" its events as its public API so others subscribe by topic without
    # importing it. Not enforced — publish is auto-namespaced + guarded at runtime;
    # subscribing to any topic is allowed.
    #   emits:      topics this plugin broadcasts — its public event API
    #   subscribes: topics it listens for, declared so the wiring is visible to operators
    emits: list[str] = field(default_factory=list)
    subscribes: list[str] = field(default_factory=list)
    # Typed event contracts (#1636) — topic → {"summary": str, "schema": dict} for
    # `emits:` entries that declared more than a bare name. `emits` above stays the
    # names-only topic list (every entry, bare or typed), so existing consumers are
    # untouched; this map carries the optional payload contract a cross-plugin
    # consumer can discover instead of reverse-engineering the emitter. Purely
    # declarative (like `capabilities`) — payloads are NOT validated at publish
    # time. See `_parse_emits`.
    emits_schemas: dict[str, dict] = field(default_factory=dict)
    # Distribution (ADR 0027) — for plugins installed from a git URL.
    #   requires_pip: declared pip deps. NOT auto-installed (install ≠ code exec);
    #     the operator installs them explicitly. Missing → clear error on enable.
    #     An entry is a bare PEP 508 spec string (a HARD dep) or a mapping
    #     ``{pkg: "pillow>=10", optional: true}`` — see ``_parse_requires_pip``.
    #   optional_pip: the optional tier (#1953) — specs from ``optional: true``
    #     entries. The plugin runs without them (lazy import, graceful
    #     degradation), so the frozen-app gate (ADR 0058 D2) warns instead of
    #     refusing, and ``install-deps`` installs them best-effort.
    #   repository/homepage: provenance, shown in the install review.
    #   min_protoagent_version: compat guard — the loader refuses to load the
    #     plugin when the host is older than declared (malformed strings only
    #     warn and load).
    requires_pip: list[str] = field(default_factory=list)
    optional_pip: list[str] = field(default_factory=list)
    #   pip_scopes: pkg name -> "host" | "runtime" (#2246). Which INTERPRETER has to be
    #     able to import the dep. ``runtime`` (the default, matching the compute-plugin
    #     pattern) means the managed Python runtime that serves ``execute_code`` children;
    #     ``host`` means this process. They have separate site-packages, so a dep the
    #     managed runtime satisfies is NOT importable in a frozen host — and a plugin whose
    #     tools import it in-process would pass the install gate and then die at tool time.
    #     Only non-default (``host``) entries are recorded; absent ⇒ runtime.
    pip_scopes: dict[str, str] = field(default_factory=dict)
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
_VALID_SETTINGS_TAB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _percent_decoded(value: str) -> str:
    """``value`` with every layer of percent-encoding removed.

    Nested on purpose: a single ``unquote`` turns ``%252e%252e`` into ``%2e%2e``,
    which a browser then decodes again into ``..``. A validator that stops after one
    pass is looking at a different string than the one the request path will be
    matched on.
    """
    while True:
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded


def _iframe_page_route(path: object) -> str:
    """Canonical route portion of a manifest iframe URL.

    Browser navigation and the ASGI request path percent-decode the route before
    auth/router matching. Normalize the manifest spelling the same way (including
    nested encodings) so validation, public-chrome exemptions, and served-route
    diagnostics cannot disagree. Query/fragment remain on the iframe URL but never
    participate in either boundary.
    """
    return _percent_decoded(str(path or "").strip().split("?", 1)[0].split("#", 1)[0])


def _parse_settings_tabs(tabs, plugin_id: str) -> list[dict]:
    """Normalize ordered ``settings_tabs`` descriptors to unique ``{id, label, …}`` mappings.

    Invalid entries are ignored with a warning so one presentation typo cannot stop
    plugin discovery. Unknown keys are preserved for forward-compatible descriptor
    extensions. ``path`` is the sandboxed plugin-owned kind (#3180) and is kept
    only when it is a root-relative page inside this plugin's exact public
    ``/plugins/<id>/...`` namespace.
    """
    if not isinstance(tabs, (list, tuple)):
        return []
    kept: list[dict] = []
    seen: set[str] = set()
    for raw in tabs:
        if not isinstance(raw, dict):
            log.warning("[plugins] %s: settings_tabs entry must be a mapping — ignored", plugin_id)
            continue
        tab_id = str(raw.get("id", "")).strip()
        label = str(raw.get("label", "")).strip()
        if not _VALID_SETTINGS_TAB_ID.fullmatch(tab_id) or not label:
            log.warning(
                "[plugins] %s: settings_tabs entry needs a safe id and non-empty label — ignored",
                plugin_id,
            )
            continue
        if tab_id in seen:
            log.warning("[plugins] %s: duplicate settings tab id %r — keeping first", plugin_id, tab_id)
            continue
        normalized = {**raw, "id": tab_id, "label": label}
        if "path" in raw:
            path = str(raw.get("path") or "").strip()
            route = _iframe_page_route(path)
            parts = route.split("/")
            root = f"/plugins/{plugin_id}/"
            if (
                not path
                or not route.startswith(root)
                or "\\" in route
                or any(part in {".", ".."} for part in parts)
            ):
                log.warning(
                    "[plugins] %s: settings tab %r Configure path %r must be a same-origin "
                    "page under /plugins/%s/... — ignored",
                    plugin_id,
                    tab_id,
                    path,
                    plugin_id,
                )
                continue
            normalized["path"] = path
        seen.add(tab_id)
        kept.append(normalized)
    return kept


def _parse_settings(settings, tabs: list[dict], plugin_id: str) -> list[dict]:
    """Keep mapping settings and drop invalid tab references to the flat fallback."""
    if not isinstance(settings, (list, tuple)):
        return []
    known_tabs = {tab["id"] for tab in tabs}
    path_tabs = {tab["id"] for tab in tabs if tab.get("path")}
    kept: list[dict] = []
    for raw in settings:
        if not isinstance(raw, dict):
            continue
        spec = dict(raw)
        if "tab" in spec:
            tab_id = str(spec.get("tab", "")).strip()
            if tab_id not in known_tabs:
                log.warning(
                    "[plugins] %s: setting %r references unknown settings tab %r — using Configuration",
                    plugin_id,
                    spec.get("key"),
                    tab_id,
                )
                spec.pop("tab", None)
            elif tab_id in path_tabs:
                log.warning(
                    "[plugins] %s: setting %r cannot target path-backed settings tab %r "
                    "— using Configuration",
                    plugin_id,
                    spec.get("key"),
                    tab_id,
                )
                spec.pop("tab", None)
            else:
                spec["tab"] = tab_id
        kept.append(spec)
    return kept


def _parse_views(views, plugin_id: str) -> list[dict]:
    """Keep view entries with an ``id`` + ``path``; warn on non-same-origin paths and
    on a ``palette`` value the console cannot read.

    Views must point at a same-origin **relative** path. A path that carries a
    scheme or host (``http(s)://``, protocol-relative ``//host``, ``localhost``, or
    a ``:PORT``) breaks the ADR 0042 fleet proxy and the same-origin postMessage
    token handshake — we log a warning but still keep the view so the author sees
    the mistake rather than a silently-missing rail icon.

    ``palette`` (ADR 0057) opts a view into the palette's inline morph and is honored
    in exactly two spellings — the literal ``"inline"``, or a ``{path: …}`` mapping
    naming a different page to morph. The console tests for precisely those, so every
    other value (``palette: true`` above all, the shape an author guesses first) is
    ignored there with no rail change and no log line — the one view mistake that
    fails silently. Drop the bad value, keep the view, and say so: ``palette: []``
    would otherwise reach a ``typeof … === "object"`` test as a truthy morph opt-in
    the author never asked for.
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
        view = dict(v)
        if "palette" in view:
            palette = view["palette"]
            morph = str(palette.get("path") or "").strip() if isinstance(palette, dict) else ""
            if palette != "inline" and not morph:
                log.warning(
                    "[plugins] %s: view %r palette %r is neither the literal 'inline' nor a "
                    "{path: …} mapping — the console reads only those two, so the inline morph "
                    "is dropped (the view itself still loads, and is already a palette entry "
                    "without opting in — `palette` only ever selects the inline morph)",
                    plugin_id,
                    v.get("id"),
                    palette,
                )
                view.pop("palette", None)
            elif morph and _NON_SAME_ORIGIN_PATH.search(morph):
                log.warning(
                    "[plugins] %s: view %r palette path %r is not same-origin relative — a "
                    "scheme/host breaks the fleet proxy + the postMessage token handshake; "
                    "use a relative path",
                    plugin_id,
                    v.get("id"),
                    morph,
                )
        kept.append(view)
    return kept


# The action vocabulary the console adapter can compile into a run() (ADR 0057 §4).
# Closed on purpose: an action type the trusted adapter cannot dispatch is a palette
# row that does nothing, so an unrecognized one is dropped rather than surfaced.
_COMMAND_ACTIONS = ("navigate", "open_view", "tool", "emit", "command")
# HTTP verbs a `tool` action may use. A typo is dropped, never coerced — guessing a
# verb for an authenticated call is how a read becomes a write.
_COMMAND_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_VALID_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# ASCII tab, LF and CR are *deleted* from a URL by the WHATWG parser as its very first
# step — before it resolves `..` — so `.<TAB>.` is not a dot-dot segment to a validator
# that reads the declared string and IS one to the `fetch` that follows:
# `new URL("/api/plugins/evil/.\t./.\t./config")` has pathname `/api/config`. The other
# C0 controls (and DEL) are never legitimate in a route either, so a route carrying any
# of them is dropped outright rather than sanitized — that is what keeps the string
# `_parse_command_route` checks identical to the one the browser will match, which is the
# whole contract the docstring below describes.
_URL_RESHAPING_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _parse_command_route(raw, plugin_id: str, *, command_id: str, label: str) -> str | None:
    """A command/provider ``route`` → its relative form under this plugin's own
    ``/api/plugins/<id>/`` namespace, or ``None`` (warned) when it escapes it.

    **This drops instead of warning-and-keeping, and that difference is the point.**
    A bad view path only blanks an iframe; a command route is compiled into an
    authenticated ``fetch`` that carries the operator bearer, an absolute URL is
    passed through unchanged by the console's ``apiUrl``, and the browser collapses
    ``/api/plugins/<id>/../../config`` to ``/api/config`` before the request is ever
    sent. Keeping a broken route would therefore publish a manifest-declared write
    against a core endpoint. The precedent to mirror is ``_parse_public_paths``:
    namespace-scoping IS the security boundary, and its trailing slash is load-bearing.

    Every check therefore runs on the string *the browser will match*, not the string
    the manifest spelled. That takes two normalizations up front, in the browser's own
    order: percent-encoding is unwrapped (``%2e%2e`` is ``..`` by the time a browser
    normalizes the path), and any URL-reshaping control character is rejected, because
    the URL parser strips tab/LF/CR *before* it resolves ``..`` — ``%2e%09%2e/%2e%09%2e``
    decodes to no dot-dot segment here and arrives as ``../..`` at request time. Only
    then is the composed path re-checked against the namespace root with
    ``posixpath.normpath`` — ``posixpath`` and not ``os.path``, because URL routes stay
    ``/``-separated on the Windows leg where ``os.path`` would treat a backslash as a
    separator and normalize a different string than the browser does.
    """
    declared = str(raw or "").strip()
    route = _percent_decoded(declared)
    root = f"/api/plugins/{plugin_id}/"
    reason = ""
    if not route:
        reason = "is empty"
    elif _URL_RESHAPING_CHARS.search(route):
        reason = (
            "carries a tab, newline or other control character — the URL parser deletes "
            "those before it resolves '..', so the route the browser requests is not the "
            "one declared"
        )
    elif _NON_SAME_ORIGIN_PATH.search(route):
        reason = "carries a scheme, host or port"
    elif route.startswith("/"):
        reason = "is absolute"
    elif "\\" in route or "?" in route or "#" in route:
        reason = "carries a backslash, query or fragment"
    elif ".." in route.split("/"):
        reason = "contains a '..' segment"
    else:
        composed = posixpath.normpath(root + route)
        if composed.startswith(root):
            return composed[len(root) :]
        reason = "normalizes outside the plugin namespace"
    log.warning(
        "[plugins] %s: command %r %s %r %s — it must be a relative route inside "
        "/api/plugins/%s/… (the console calls it with the operator bearer attached); dropped",
        plugin_id,
        command_id,
        label,
        declared,
        reason,
        plugin_id,
    )
    return None


def _parse_command_topic(raw, plugin_id: str, *, command_id: str) -> str | None:
    """An ``emit`` action ``topic`` forced into THIS plugin's namespace, or ``None``.

    A bare name is prefixed with the plugin id; a dotted topic must already start with
    it. Forcing the namespace here is the whole guard: the bus check on
    ``POST /api/events/publish`` (ADR 0039) only asks that a topic be dotted and
    wildcard-free — it never asks *who* is publishing — so a manifest free to name any
    namespace could forge another plugin's events, e.g. ``otherplugin.wipe``.
    """
    topic = str(raw or "").strip()
    segments = topic.split(".")
    if (
        topic
        and not any(c.isspace() for c in topic)
        and "*" not in topic
        and "#" not in topic
        and all(segments)
        and (len(segments) == 1 or segments[0] == plugin_id)
    ):
        return topic if len(segments) > 1 else f"{plugin_id}.{topic}"
    log.warning(
        "[plugins] %s: command %r emit topic %r is outside the plugin's own namespace "
        "(use a bare name, or prefix it with %r) — dropped",
        plugin_id,
        command_id,
        topic,
        f"{plugin_id}.",
    )
    return None


def _parse_command_action(raw, plugin_id: str, *, command_id: str, view_ids: set[str], label: str) -> dict | None:
    """One declarative ``action`` → the normalized mapping the console dispatches on.

    Returns ``None`` (warned) for anything the adapter could not run safely. The result
    is rebuilt from validated pieces rather than passed through, so what ships is
    exactly the closed vocabulary — a normalized route, a namespaced topic, a view id
    this manifest actually declares.
    """
    if not isinstance(raw, dict):
        log.warning("[plugins] %s: command %r %s must be a mapping — dropped", plugin_id, command_id, label)
        return None
    kind = str(raw.get("type", "") or "").strip()
    if kind not in _COMMAND_ACTIONS:
        log.warning(
            "[plugins] %s: command %r %s type %r is not one of %s — dropped",
            plugin_id,
            command_id,
            label,
            kind,
            ", ".join(_COMMAND_ACTIONS),
        )
        return None
    if kind in ("navigate", "open_view"):
        view = str(raw.get("view", "") or "").strip()
        if view not in view_ids:
            log.warning(
                "[plugins] %s: command %r %s targets view %r, which this manifest does not "
                "declare — a command may only open its own plugin's views; dropped",
                plugin_id,
                command_id,
                label,
                view,
            )
            return None
        action = {"type": kind, "view": view}
        # `open_view` IS the inline morph (ADR 0057 §2C/§4 spell it
        # `{type: open_view, view, inline: true}`, and `navigate` is the non-inline half),
        # so `inline` is normalized ON rather than read. A manifest that merely omitted it
        # would otherwise compile to an action the adapter's `isInlineView` filter skips,
        # leaving its `ctx.enter(<view id>)` pointing at a palette view nobody registered —
        # and the DS palette renders `null` for an unknown view id, so the whole palette
        # blanks (until Escape pops the frame) instead of the command doing nothing.
        if kind == "open_view":
            action["inline"] = True
        return action
    if kind == "tool":
        route = _parse_command_route(raw.get("route"), plugin_id, command_id=command_id, label=f"{label} route")
        if route is None:
            return None
        method = str(raw.get("method", "") or "POST").strip().upper()
        if method not in _COMMAND_METHODS:
            log.warning(
                "[plugins] %s: command %r %s method %r is not one of %s — dropped",
                plugin_id,
                command_id,
                label,
                method,
                ", ".join(_COMMAND_METHODS),
            )
            return None
        return {"type": "tool", "route": route, "method": method}
    if kind == "emit":
        topic = _parse_command_topic(raw.get("topic"), plugin_id, command_id=command_id)
        if topic is None:
            return None
        action = {"type": "emit", "topic": topic}
        data = raw.get("data")
        if data is not None:
            if not isinstance(data, dict):
                log.warning(
                    "[plugins] %s: command %r %s data must be a mapping — dropped",
                    plugin_id,
                    command_id,
                    label,
                )
                return None
            action["data"] = data
        return action
    target = str(raw.get("command", "") or "").strip()
    if not target:
        log.warning(
            "[plugins] %s: command %r %s needs the id of the command to run — dropped",
            plugin_id,
            command_id,
            label,
        )
        return None
    return {"type": "command", "command": target}


def _parse_command_provider(raw, plugin_id: str, *, command_id: str, view_ids: set[str]) -> dict | None:
    """A ``provider`` block → ``{route, result_action}``, or ``None`` (warned).

    A provider is a live-search row: the console queries ``route`` as the operator
    types and turns each result into a command running ``result_action``. Both halves
    go through the same validation as a fixed command — the query is an authenticated
    call, and a result row is a dispatched action.

    ``result_action`` is REQUIRED. A result row is inert data — a label the plugin's
    route returned — and the DS ``Command`` each row becomes has no *optional* ``run``,
    so the manifest's declared action is the only thing that can fire when the operator
    picks one. A provider without it is the "half a command" state ``_parse_commands``
    exists to keep out of the palette: a search that answers and then does nothing.
    """
    if not isinstance(raw, dict):
        log.warning("[plugins] %s: command %r provider must be a mapping — dropped", plugin_id, command_id)
        return None
    route = _parse_command_route(raw.get("route"), plugin_id, command_id=command_id, label="provider route")
    if route is None:
        return None
    if "result_action" not in raw:
        log.warning(
            "[plugins] %s: command %r provider declares no result_action — its result rows "
            "would have nothing to run when they are picked; dropped",
            plugin_id,
            command_id,
        )
        return None
    result = _parse_command_action(
        raw["result_action"],
        plugin_id,
        command_id=command_id,
        view_ids=view_ids,
        label="provider result_action",
    )
    if result is None:
        return None
    return {"route": route, "result_action": result}


def _chained_command_ids(command: dict) -> list[str]:
    """Ids a command's ``command``-type actions hand control to."""
    actions = [command.get("action"), (command.get("provider") or {}).get("result_action")]
    return [a["command"] for a in actions if isinstance(a, dict) and a.get("type") == "command"]


def _parse_commands(entries, plugin_id: str, views: list[dict]) -> list[dict]:
    """Parse ``commands:`` → validated palette entries (ADR 0057 §3).

    Strict by design, and deliberately unlike ``_parse_views``: a view is chrome, so a
    malformed one is kept with a warning; a command is a *dispatch* — the console
    compiles its action into an authenticated request, an event publish, or another
    command — so anything that fails validation is DROPPED. Half a command is worse
    than none: it puts a row in the palette that fires something the author didn't
    write. An entry needs a safe ``id``, a ``title``, and at least one of ``action`` /
    ``provider``; if a declared half fails, the whole entry goes, so what survives is
    fully dispatchable.

    Every escape hatch is closed here rather than in the console: routes are confined
    to /api/plugins/<id>/… (``_parse_command_route``), emit topics to the plugin's own
    event namespace (``_parse_command_topic``), ``navigate`` / ``open_view`` to views
    this manifest declares, and ``command`` chaining to entries in this same list —
    resolved to a fixed point, so a chain into a dropped command is dropped too.
    """
    if not isinstance(entries, (list, tuple)):
        return []
    view_ids = {str(v.get("id")) for v in views}
    kept: list[dict] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            log.warning("[plugins] %s: commands entry must be a mapping — ignored", plugin_id)
            continue
        command_id = str(raw.get("id", "") or "").strip()
        title = str(raw.get("title", "") or "").strip()
        if not _VALID_COMMAND_ID.fullmatch(command_id) or not title:
            # Name both halves: this is the one command warning that fires BEFORE there
            # is a valid id to identify the entry by, and an author staring at a manifest
            # of a dozen commands needs something to grep for.
            log.warning(
                "[plugins] %s: commands entry (id %r, title %r) needs a safe id (%s) and a "
                "non-empty title — ignored",
                plugin_id,
                command_id,
                title,
                _VALID_COMMAND_ID.pattern,
            )
            continue
        if command_id in seen:
            log.warning("[plugins] %s: duplicate command id %r — keeping first", plugin_id, command_id)
            continue
        has_action, has_provider = "action" in raw, "provider" in raw
        if not (has_action or has_provider):
            log.warning(
                "[plugins] %s: command %r declares neither an action nor a provider — "
                "there is nothing for the palette to run; ignored",
                plugin_id,
                command_id,
            )
            continue
        action = provider = None
        if has_action:
            action = _parse_command_action(
                raw["action"], plugin_id, command_id=command_id, view_ids=view_ids, label="action"
            )
            if action is None:
                continue
        if has_provider:
            provider = _parse_command_provider(raw["provider"], plugin_id, command_id=command_id, view_ids=view_ids)
            if provider is None:
                continue
        entry: dict = {"id": command_id, "title": title}
        for key in ("hint", "icon", "group"):
            value = str(raw.get(key, "") or "").strip()
            if value:
                entry[key] = value
        # A bare string is NOT a keyword list — it is a Sequence, so iterating `find`
        # would ship ["f", "i", "n", "d"] — and an empty list stays off the entry
        # entirely, like every other optional field above.
        raw_keywords = raw.get("keywords")
        keywords = (
            [k for k in (str(x).strip() for x in raw_keywords) if k]
            if isinstance(raw_keywords, (list, tuple))
            else []
        )
        if keywords:
            entry["keywords"] = keywords
        if action is not None:
            entry["action"] = action
        if provider is not None:
            entry["provider"] = provider
        seen.add(command_id)
        kept.append(entry)
    # Resolve `command` chaining last, once every id that survived is known. A chain
    # is kept only if it provably ENDS: grow the terminating set from the commands
    # that chain nowhere, and whatever never joins it either points at a command this
    # manifest doesn't declare (an escape out of the plugin's own namespace) or loops
    # (a --> b --> a the console would run until the tab froze).
    terminating = {c["id"] for c in kept if not _chained_command_ids(c)}
    while True:
        grown = {c["id"] for c in kept if all(t in terminating for t in _chained_command_ids(c))}
        if grown == terminating:
            break
        terminating = grown
    for c in kept:
        if c["id"] in terminating:
            continue
        unknown = sorted(set(_chained_command_ids(c)) - seen)  # `seen` = everything that parsed
        log.warning(
            "[plugins] %s: command %r chain does not end — %s; a command may only run another "
            "command this manifest declares, and the chain has to terminate; dropped",
            plugin_id,
            c["id"],
            f"it names {', '.join(unknown)}, which this manifest does not declare"
            if unknown
            else "it loops, or leads only into commands that were themselves dropped",
        )
    return [c for c in kept if c["id"] in terminating]


# A plugin id namespaces its routes (``/plugins/<id>/``, ``/api/plugins/<id>/``)
# and its config section, so it must be a safe slug AND must not shadow a core
# ``/api/plugins/<verb>`` management route — otherwise its ``public_paths`` could
# prefix-match and exempt that core route (e.g. install = RCE) from the auth gate.
_VALID_PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_RESERVED_PLUGIN_IDS = frozenset({"install", "installed", "sync", "updates", "catalog", "enabled"})


def _parse_public_paths(paths, plugin_id: str, *, kind: str = "public_path") -> list[str]:
    """Keep auth-exempt paths that live under THIS plugin's namespace SUBTREE
    (``/plugins/<id>/…`` or ``/api/plugins/<id>/…``); drop + warn on anything else.

    Namespace-scoping is the security boundary: a plugin can exempt only its own
    routes from the auth gate, never a core path like ``/api/config`` or the core
    ``/api/plugins/<verb>`` routes. The trailing slash is load-bearing — without
    it, id ``install`` would prefix-match the core ``/api/plugins/install`` route.

    ``federation_paths`` (#2747) share this exact boundary — ``kind`` only labels
    the warning so an operator can tell which manifest key was rejected."""
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
                "[plugins] %s: %s %r is outside the plugin namespace "
                "(/plugins/%s/… or /api/plugins/%s/…) — ignored",
                plugin_id, kind, s, plugin_id, plugin_id,
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
            p = _iframe_page_route(c)
            if p:
                out.append(p)
    return out


def _settings_tab_public_paths(tabs: list[dict]) -> list[str]:
    """Public page chrome for path-backed Configure tabs (#3180).

    Validation in ``_parse_settings_tabs`` has already confined every path to the
    declaring plugin's /plugins/<id>/ subtree. Query/fragment select page state;
    the middleware exemption is the underlying route only.
    """
    return [_iframe_page_route(tab["path"]) for tab in tabs if tab.get("path")]


def _load_schema_ref(ref: str, plugin_dir: Path, plugin_id: str, topic: str) -> dict | None:
    """Read a schema ``$ref`` file from inside the plugin directory → mapping.

    The ref is resolved relative to the plugin dir and must stay inside it (a
    ``../…`` escape is refused — the manifest must not read arbitrary host files).
    The file is parsed with ``yaml.safe_load`` (JSON is a YAML subset, so plain
    ``.json`` schema files work). Every failure — escape, missing file, parse
    error, non-mapping content — warns and returns ``None`` so the entry keeps its
    names-only behavior; a bad ref never fails the plugin load.
    """
    root = plugin_dir.resolve()
    try:
        target = (root / ref).resolve()
        if not target.is_relative_to(root):
            log.warning(
                "[plugins] %s: emits %r schema $ref %r escapes the plugin directory — ignored",
                plugin_id, topic, ref,
            )
            return None
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        log.warning(
            "[plugins] %s: emits %r schema $ref %r is unreadable (%s) — keeping the "
            "topic name only",
            plugin_id, topic, ref, exc,
        )
        return None
    if not isinstance(loaded, dict):
        log.warning(
            "[plugins] %s: emits %r schema $ref %r is not a mapping — keeping the topic name only",
            plugin_id, topic, ref,
        )
        return None
    return loaded


def _resolve_emit_schema(schema, plugin_dir: Path, plugin_id: str, topic: str) -> dict | None:
    """One entry's ``schema:`` value → JSON-schema mapping (or ``None``, warned).

    Accepted forms: an inline mapping (kept verbatim), a mapping whose ONLY key is
    ``$ref`` pointing to a file inside the plugin dir, or a bare string as
    shorthand for that ``$ref``.
    """
    if isinstance(schema, str):
        return _load_schema_ref(schema.strip(), plugin_dir, plugin_id, topic)
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if set(schema.keys()) == {"$ref"} and isinstance(ref, str):
            return _load_schema_ref(ref.strip(), plugin_dir, plugin_id, topic)
        return schema
    log.warning(
        "[plugins] %s: emits %r schema must be a mapping or a $ref path, got %s — "
        "keeping the topic name only",
        plugin_id, topic, type(schema).__name__,
    )
    return None


def _parse_emits(entries, plugin_dir: Path, plugin_id: str) -> tuple[list[str], dict[str, dict]]:
    """Parse ``emits:`` → ``(topic names, topic → declared contract)`` (#1636).

    An entry is either a bare topic string (today's behavior, unchanged) or a
    mapping with ``topic`` plus an optional ``summary`` and/or ``schema``:

    .. code-block:: yaml

        emits:
          - spacetraders.window_closed          # bare name — still fine
          - topic: spacetraders.trade_executed
            summary: A hauler completed a buy→sell leg
            schema: {type: object, required: [route, profit], properties: {...}}
          - topic: spacetraders.ship_purchased
            schema: {$ref: events/ship_purchased.json}   # file inside the plugin repo

    Both forms contribute the topic NAME to the first return (what ``emits``
    consumers already read); entries that declare a summary/schema also land in
    the contract map. Purely declarative — nothing validates payloads at publish
    time. Malformed entries warn and degrade to names-only (or are skipped when
    there's no usable topic); they never fail the manifest load.
    """
    if not isinstance(entries, (list, tuple)):
        return [], {}
    names: list[str] = []
    schemas: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict):
            topic = str(entry.get("topic", "") or "").strip()
            if not topic:
                log.warning(
                    "[plugins] %s: emits entry %r has no 'topic' — skipped", plugin_id, entry
                )
                continue
            names.append(topic)
            contract: dict = {}
            summary = entry.get("summary")
            if summary:
                contract["summary"] = str(summary)
            if "schema" in entry:
                schema = _resolve_emit_schema(entry["schema"], plugin_dir, plugin_id, topic)
                if schema is not None:
                    contract["schema"] = schema
            if contract:
                schemas[topic] = contract
        else:
            names.append(str(entry))
    return names, schemas


#: Valid ``scope:`` values on a ``requires_pip`` mapping entry (#2246).
_PIP_SCOPES = ("host", "runtime")


def _parse_requires_pip(entries, plugin_id: str) -> tuple[list[str], list[str], dict[str, str]]:
    """Parse ``requires_pip:`` → ``(hard specs, optional specs, scopes)`` (#1953, #2246).

    An entry is either a bare PEP 508 spec string (today's behavior, unchanged —
    a HARD dep) or a mapping ``{pkg: "pillow>=10", optional: true, scope: host}``:

    .. code-block:: yaml

        requires_pip:
          - "httpx>=0.27"                            # hard, runtime-scoped (default)
          - { pkg: "pillow>=10", optional: true }    # optional tier
          - { pkg: "numpy", scope: host }            # imported IN-PROCESS by the plugin

    The optional tier is for a dep the plugin degrades gracefully without (a
    lazy import + a readable tool error naming the fix): the frozen-app gate
    (ADR 0058 D2) warns instead of refusing when it's missing, and
    ``install-deps`` installs it best-effort.

    ``scope`` says which INTERPRETER must be able to import the dep. ``runtime``
    (the default, matching the compute-plugin pattern) is the managed Python runtime
    serving ``execute_code`` children; ``host`` is this process. They have separate
    site-packages, so declaring nothing and relying on the runtime is wrong for a
    plugin whose *tools* import the dep in-process — that combination passed the
    frozen install gate and then died with ``ModuleNotFoundError`` at every tool call.
    Only non-default (``host``) entries are recorded, so an unscoped manifest parses
    to an empty mapping and behaves exactly as before.

    A mapping without ``optional: true`` is just a hard dep spelled long-form; a
    mapping without a usable ``pkg`` warns and is skipped — it never fails the manifest
    load (mirroring ``_parse_emits``). An unrecognized ``scope`` warns and falls back to
    the default rather than rejecting the plugin.
    """
    if not isinstance(entries, (list, tuple)):
        return [], [], {}
    hard: list[str] = []
    optional: list[str] = []
    scopes: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, dict):
            pkg = str(entry.get("pkg", "") or "").strip()
            if not pkg:
                log.warning(
                    "[plugins] %s: requires_pip entry %r has no 'pkg' — skipped", plugin_id, entry
                )
                continue
            (optional if entry.get("optional") else hard).append(pkg)
            raw_scope = str(entry.get("scope", "") or "").strip().lower()
            if raw_scope and raw_scope not in _PIP_SCOPES:
                log.warning(
                    "[plugins] %s: requires_pip entry %r has unknown scope %r (expected %s) — "
                    "treating as 'runtime'",
                    plugin_id,
                    pkg,
                    raw_scope,
                    " or ".join(_PIP_SCOPES),
                )
                raw_scope = ""
            if raw_scope == "host":
                scopes[_pip_pkg_name(pkg)] = "host"
        else:
            hard.append(str(entry))
    return hard, optional, scopes


def _pip_pkg_name(spec: str) -> str:
    """Distribution name out of a PEP 508 spec — ``"pillow>=10,<11"`` → ``"pillow"``.

    Mirrors ``installer._dep_pkg_name``; duplicated rather than imported because the
    manifest parser must stay importable without the installer (a plugin's own test
    suite loads manifests host-free)."""
    return re.split(r"[<>=!~\[; ]", str(spec).strip(), maxsplit=1)[0].strip()


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
    settings_tabs = _parse_settings_tabs(data.get("settings_tabs"), pid)
    settings = _parse_settings(data.get("settings"), settings_tabs, pid)
    views = _parse_views(data.get("views"), pid)
    # Palette commands (ADR 0057) are parsed AFTER views because navigate/open_view
    # actions may only target a view this same manifest declares.
    commands = _parse_commands(data.get("commands"), pid, views)
    # public_paths = explicitly-declared exempt paths PLUS every iframe page's
    # path (rail views and Configure tabs are public chrome). All run
    # through the namespace validator; dict.fromkeys dedupes while preserving order
    # (a view path a manifest also lists explicitly collapses to one).
    public_paths = list(
        dict.fromkeys(
            [
                *_parse_public_paths(data.get("public_paths"), pid),
                *_parse_public_paths(_view_public_paths(views), pid),
                *_parse_public_paths(_settings_tab_public_paths(settings_tabs), pid),
            ]
        )
    )
    # `commands` are deliberately absent from the list above AND from federation_paths
    # below. A view PAGE is public chrome (a plain iframe navigation that cannot carry
    # the bearer); a command ROUTE is an authenticated API call the console makes WITH
    # the bearer, so auto-exempting one would turn a palette entry into an
    # unauthenticated write.
    #
    # federation_paths (#2747): same namespace validator, separate list — these
    # lower the tier ceiling, they never exempt auth, so a view page is NOT
    # auto-added here the way it is for public_paths.
    federation_paths = list(dict.fromkeys(_parse_public_paths(data.get("federation_paths"), pid, kind="federation_path")))
    emits, emits_schemas = _parse_emits(data.get("emits"), plugin_dir, pid)
    subscribes = data.get("subscribes")
    requires_pip, optional_pip, pip_scopes = _parse_requires_pip(data.get("requires_pip"), pid)
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
        settings=settings,
        settings_tabs=settings_tabs,
        test=bool(data.get("test", False)),
        guide_url=str(data.get("guide_url", "") or "").strip(),
        views=views,
        commands=commands,
        public_paths=public_paths,
        federation_paths=federation_paths,
        emits=emits,
        emits_schemas=emits_schemas,
        subscribes=[str(x) for x in subscribes] if isinstance(subscribes, (list, tuple)) else [],
        requires_pip=requires_pip,
        optional_pip=optional_pip,
        pip_scopes=pip_scopes,
        repository=str(data.get("repository", "")).strip(),
        homepage=str(data.get("homepage", "")).strip(),
        min_protoagent_version=str(data.get("min_protoagent_version", "")).strip(),
    )
