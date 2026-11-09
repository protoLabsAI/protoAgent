"""`protoagent plugin preview <dir>` — a host-free console VIEW harness (#4809, ADR 0034/0039).

The sibling to ``plugin check`` (#4809 part 1, the static lint): where ``check`` reads a
plugin without importing it, ``preview`` RUNS the plugin's real view inside the real,
prebuilt operator console — with **no protoAgent server** behind it. To debug cadview's
freeze before this, you had to drive Playwright WebKit against the LIVE hub and copy the
desktop's WKWebView localStorage (operator token + ``protoagent.ui:<id>`` layout) by hand.
This reproduces that setup host-free.

How it fits together (the console is the front door; Python only serves the plugin):

  browser ──▶ node e2e mock backend (apps/web/e2e/mock-server.mjs) ──▶ built console (apps/web/dist)
                 │  • canned operator API + A2A stream + the #4283 bus tap
                 │  • GET /api/runtime/status with THIS plugin's views spliced in (PLUGIN_PREVIEW_PLUGIN)
                 │  • seeds the layout into protoagent.ui:<id> localStorage (PLUGIN_PREVIEW_LAYOUT)
                 └─▶ proxies /api/plugins/<id>/* and /plugins/<id>/* to ◀── this module's uvicorn app
                        (the plugin's REAL routers, mounted through the testkit's FakeRegistry)

So the plugin's routers answer for real (a router contract bug surfaces), its view page and
``public_paths`` serve from the plugin, the bus tap lets a test publish and assert received
frames, and a layout seed can mount one view in the surface AND the right panel at once — the
cadview freeze shape. It mounts NO host: ``graph.*``/``knowledge.*`` imports resolve to the
testkit's stubs, exactly as a plugin's own host-free unit tests run.

Stays out of ``server/`` on purpose (the import-layering invariant — ``graph`` never imports
``server``): the router mounting here is the handful of ``include_router`` lines the real
``server.plugin_wiring`` does, not a call into it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.plugins.preview")

# The persisted UI-layout zustand store version (apps/web/src/state/uiStore.ts `version:`).
# The seed carries it so the console's persist layer treats the blob as current and skips the
# migration chain (which would otherwise rewrite a version-less seed). Bump in lockstep if the
# store version does — a parity note lives next to the e2e layout seed.
_UI_STORE_VERSION = 14


class ConsoleBundleMissing(RuntimeError):
    """The built console (apps/web/dist) is absent — raised with the build command to run.
    Never built silently: the console build is a multi-minute Node step the operator opts into."""


@dataclass
class PreviewApp:
    """What ``build_preview_app`` hands back: the FastAPI app carrying the plugin's REAL
    routers, and the view descriptor the console discovers the plugin's views through."""

    app: object  # fastapi.FastAPI — typed loosely so importing this module needs no FastAPI
    descriptor: dict
    registry: object  # the FakeRegistry, so a caller can assert what register() contributed


def app_root() -> Path:
    """The read-only asset root (source/wheel/frozen) — where ``apps/web`` lives."""
    from infra.paths import app_root as _app_root

    return _app_root()


def console_dist_dir() -> Path:
    return app_root() / "apps" / "web" / "dist"


def mock_server_path() -> Path:
    return app_root() / "apps" / "web" / "e2e" / "mock-server.mjs"


def console_build_command() -> str:
    """The exact command the operator runs to produce apps/web/dist (PROTO.md gate)."""
    return "npm ci && npm run build --workspace @protoagent/web"


def locate_console_bundle() -> Path:
    """The built console dir, or raise ``ConsoleBundleMissing`` naming the build command.
    The marker is ``index.html`` — an empty/partial ``dist`` is as unusable as a missing one."""
    dist = console_dist_dir()
    if not (dist / "index.html").is_file():
        raise ConsoleBundleMissing(
            f"the console bundle is not built at {dist}. Build it first:\n"
            f"    {console_build_command()}\n"
            "(the preview harness never builds it silently — it's a multi-minute Node step)."
        )
    return dist


def build_preview_descriptor(manifest) -> dict:
    """The view descriptor the console discovers the plugin through — the subset of
    ``/api/runtime/status``'s plugin entry that drives the rail (ADR 0026): ``views`` (with
    each view's ``placement``) and the ``public_paths`` that keep the view pages auth-exempt.
    Derived straight from the manifest, so what the harness shows is what the loader would."""
    return {
        "id": manifest.id,
        "name": manifest.name,
        "version": getattr(manifest, "version", "0.0.0"),
        "public_paths": list(getattr(manifest, "public_paths", []) or []),
        # `views` are already plain dicts (manifest._parse_views) — {id,label,icon,path,placement?}.
        "views": [dict(v) for v in (getattr(manifest, "views", []) or [])],
    }


def build_preview_app(plugin_dir, plugin_id: str | None = None) -> PreviewApp:
    """Load the plugin HOST-FREE and mount its REAL routers on a fresh FastAPI app.

    Uses the testkit exactly as a plugin's own CI does: ``install_host_stubs`` registers the
    ``graph.*``/``knowledge.*`` stand-ins, ``load_plugin`` imports the dir as a package so its
    relative imports resolve, and a ``FakeRegistry`` captures what ``register()`` contributes.
    Each captured router is mounted at the prefix the plugin asked for — defaulting to
    ``/plugins/<id>`` when it passed none, the same default the real registry applies — so the
    plugin's own view page and ``/api/plugins/<id>/…`` data routes answer for real behind the
    console. No ``server`` import: this is the include_router lines, not server.plugin_wiring.
    """
    from fastapi import FastAPI

    from graph.plugins.manifest import load_manifest
    from graph.plugins.testkit import FakeRegistry, install_host_stubs, load_plugin

    plugin_dir = Path(plugin_dir).resolve()
    manifest = load_manifest(plugin_dir)
    if manifest is None:
        raise RuntimeError(
            f"no valid plugin manifest at {plugin_dir / 'protoagent.plugin.yaml'} "
            "(need a readable protoagent.plugin.yaml with id + name)"
        )
    pid = plugin_id or manifest.id
    descriptor = build_preview_descriptor(manifest)

    # Match the runtime loader's entry selection without importing the host loader.
    entries = (manifest.entrypoint,) if manifest.entrypoint else ("__init__.py", "plugin.py")
    entry = next((name for name in entries if (plugin_dir / name).exists()), None)
    if entry is None:
        raise RuntimeError(f"plugin {pid!r} has no entry module (expected {' or '.join(entries)})")

    install_host_stubs()
    module = load_plugin(plugin_dir, pid, entry=entry)
    register = getattr(module, "register", None)
    if not callable(register):
        raise RuntimeError(f"plugin {pid!r} has no register(registry) — nothing to preview")
    registry = FakeRegistry(
        plugin_id=pid,
        plugin_dir=plugin_dir,
        public_paths=descriptor["public_paths"],
    )
    register(registry)

    app = FastAPI(title=f"plugin-preview:{pid}", docs_url=None, redoc_url=None, openapi_url=None)
    for prefix, router in registry.routers:
        # FakeRegistry stores register_router's raw (prefix, router); None means "the default",
        # which the real registry resolves to /plugins/<id> (registry.register_router).
        eff = f"/plugins/{pid}" if prefix is None else str(prefix)
        app.include_router(router, prefix=eff)
    return PreviewApp(app=app, descriptor=descriptor, registry=registry)


def layout_seed_payload(descriptor: dict, layout: dict | None) -> dict:
    """The persisted zustand blob to drop into the console's ``protoagent.ui:<id>`` key.

    ``layout`` names which of the plugin's views mount where — ``{"surface": <viewId>,
    "right": <viewId>, "left": [...], "bottom": [...]}`` — so a test (or an operator) can mount
    one view as the main stage AND another in the right panel at once (the cadview freeze
    shape). ``None`` falls back to the first rail view as the surface and the first
    ``placement: right`` view in the right panel, so a bare ``preview`` still shows something.

    View ids resolve against the descriptor and are stamped to the console's surface keys
    (``plugin:<id>:<viewId>``). The returned dict is the full ``{state, version}`` persist
    entry; ``version`` short-circuits the migration chain so the seed lands verbatim."""
    pid = descriptor["id"]
    views = descriptor.get("views") or []
    ids = {str(v.get("id")) for v in views if v.get("id")}

    def key(view_id: str | None) -> str | None:
        vid = str(view_id) if view_id is not None else None
        return f"plugin:{pid}:{vid}" if vid and vid in ids else None

    # Defaults when no layout is given: first non-right view on the stage, first right view docked.
    first_rail = next((v["id"] for v in views if v.get("placement") not in ("right", "bottom")), None)
    first_right = next((v["id"] for v in views if v.get("placement") == "right"), None)

    layout = layout or {}
    surface = key(layout.get("surface")) or key(first_rail)
    right = key(layout.get("right")) or key(first_right)
    left_extra = [k for v in (layout.get("left") or []) if (k := key(v))]
    right_extra = [k for v in (layout.get("right_panels") or []) if (k := key(v))]
    bottom = [k for v in (layout.get("bottom") or []) if (k := key(v))]

    # railOrder fully REPLACES the store default, so keep chat + the work hub present (the
    # console self-heals any other missing core surface) alongside the seeded plugin views.
    left = ["chat", *dict.fromkeys([*left_extra, *( [surface] if surface else [] )])]
    right_rail = ["work", *dict.fromkeys([*right_extra, *( [right] if right else [] )])]

    state: dict = {"railOrder": {"left": left, "right": right_rail, "bottom": bottom, "hidden": []}}
    if surface:
        state["surface"] = surface
    if right:
        state["rightPanel"] = right
    return {"state": state, "version": _UI_STORE_VERSION}


def _storage_key(agent_slug: str) -> str:
    """The localStorage key the console persists its layout under (uiStore.ts `_layoutStorage`):
    ``protoagent.ui`` on the host console, ``protoagent.ui:<slug>`` under a fleet/agent slug."""
    return f"protoagent.ui:{agent_slug}" if agent_slug else "protoagent.ui"


def write_layout_seed_file(path, descriptor: dict, layout: dict | None, agent_slug: str) -> dict:
    """Write the ``{key, value}`` the mock injects into the booting SPA's localStorage — the
    storage key plus the JSON-encoded persist blob. Returns the written dict (for tests)."""
    seed = {
        "key": _storage_key(agent_slug),
        "value": json.dumps(layout_seed_payload(descriptor, layout)),
    }
    Path(path).write_text(json.dumps(seed), encoding="utf-8")
    return seed


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_preview(
    plugin_dir,
    *,
    port: int = 4319,
    layout: dict | None = None,
    open_browser: bool = False,
    agent_slug: str | None = None,
) -> int:
    """Serve the built console against the e2e mock backend with ``plugin_dir``'s REAL routers,
    its views, the #4283 bus tap and an optional ``layout`` seed — no protoAgent host.

    Returns the mock backend's exit code (it runs in the foreground until Ctrl-C). Raises
    ``ConsoleBundleMissing`` when the console isn't built (naming the build command — never
    builds it silently). ``layout`` is the view→dock map (see ``layout_seed_payload``);
    ``agent_slug`` (default: the plugin id) is the ``protoagent.ui:<slug>`` the seed targets
    and the ``/app/agent/<slug>/`` the browser opens.
    """
    import uvicorn

    dist = locate_console_bundle()  # raises with the build command if absent
    mock = mock_server_path()
    if not mock.is_file():
        raise RuntimeError(f"the e2e mock backend is missing at {mock} — is this a full checkout?")

    preview = build_preview_app(plugin_dir)
    pid = preview.descriptor["id"]
    slug = agent_slug or pid

    # Serve the plugin's REAL routers on a loopback port, in a daemon thread — the mock proxies
    # /api/plugins/<id>/* and /plugins/<id>/* here.
    plugin_port = _find_free_port()
    server = uvicorn.Server(
        uvicorn.Config(preview.app, host="127.0.0.1", port=plugin_port, log_level="warning")
    )
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Hand the mock everything it needs via env (all gated — an unset var leaves the mock's
    # default behaviour untouched, so the existing e2e specs are unaffected):
    tmp = Path(tempfile.mkdtemp(prefix="protoagent-plugin-preview-"))
    desc_file = tmp / "plugin.json"
    desc_file.write_text(json.dumps(preview.descriptor), encoding="utf-8")
    layout_file = tmp / "layout.json"
    write_layout_seed_file(layout_file, preview.descriptor, layout, slug)

    env = dict(os.environ)
    env["PLUGIN_PREVIEW_URL"] = f"http://127.0.0.1:{plugin_port}"
    env["PLUGIN_PREVIEW_PLUGIN"] = str(desc_file)
    env["PLUGIN_PREVIEW_LAYOUT"] = str(layout_file)

    url = f"http://127.0.0.1:{port}/app/agent/{slug}/"
    print(f"✓ previewing plugin {pid!r} (no host) at {url}")
    print(f"  console bundle: {dist}")
    print(f"  plugin routers: http://127.0.0.1:{plugin_port}  ({len(preview.registry.routers)} router(s))")
    print("  Ctrl-C to stop.")
    if open_browser:
        # The mock prints its own readiness line; open shortly after it binds the port.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — opening a browser is best-effort
            pass

    proc = subprocess.run([_node_bin(), str(mock), str(port)], env=env)
    return proc.returncode


def _node_bin() -> str:
    """The Node binary to run the mock with — ``$NODE`` override, else ``node`` on PATH."""
    return os.environ.get("NODE") or "node"
