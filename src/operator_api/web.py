"""Static serving helpers for the React operator console."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


def mount_ds_plugin_kit(app, dist_dir: str | Path) -> bool:
    """Serve the design-system plugin-kit at ``/_ds/plugin-kit.{css,js}`` — in EVERY
    UI tier, including ``--ui none`` fleet members.

    Plugin iframe views ``<link>``/``<script>`` these same-origin paths (alongside
    their ``/plugins/<id>/…`` routes) so a view always matches the console's installed
    DS version instead of pinning a CDN copy. This is mounted **independently of the
    full console SPA** precisely because a fleet MEMBER (``--ui none``) serves its
    plugins' view pages but never mounts the console — without the kit those proxied
    views render with no design system (the "borked styling" desync; see
    ``docs/dev/version-coherence.md`` Axis 3). Emitted to ``dist/_ds/`` by the web
    build (``apps/web/scripts/copy-plugin-kit.mjs``). Returns True when at least one
    kit asset was served; False when the build output is absent (no ``npm run build``
    yet) — the ``/_ds`` route then 404s and views fall back to their own defaults.
    """
    dist = Path(dist_dir).resolve()
    mounted = False

    ds_kit_css = dist / "_ds" / "plugin-kit.css"
    if ds_kit_css.is_file():

        @app.get("/_ds/plugin-kit.css", include_in_schema=False)
        async def _ds_plugin_kit_css() -> FileResponse:
            return FileResponse(str(ds_kit_css), media_type="text/css")

        mounted = True

    ds_kit_js = dist / "_ds" / "plugin-kit.js"
    if ds_kit_js.is_file():

        @app.get("/_ds/plugin-kit.js", include_in_schema=False)
        async def _ds_plugin_kit_js() -> FileResponse:
            return FileResponse(str(ds_kit_js), media_type="application/javascript")

        mounted = True

    return mounted


log = logging.getLogger(__name__)

# ── The console page's Content-Security-Policy (bd-9ih.13) ──────────────────────────────────
# The directives are built with the console (apps/web/csp-policy.json, plus the streaming
# preview's script hash) and land in ``dist/csp.json``. ``PROTOAGENT_CONSOLE_CSP`` picks how
# it is sent: ``report`` (the default — the browser enforces nothing and reports what it would
# have blocked to ``/csp-report``), ``enforce``, or ``off``.
CSP_MODE_ENV = "PROTOAGENT_CONSOLE_CSP"
CSP_REPORT_PATH = "/csp-report"
_CSP_MODES = ("report", "enforce", "off")
# The directives that also take the agent's own public origins (``A2A_PUBLIC_URL`` and
# ``network.public_console_url``): signed media links handed to clients outside the box are
# minted against the first, and a paired phone checks whether the agent answers at its other
# addresses (the offline notice) before suggesting one.
_PUBLIC_ORIGIN_DIRECTIVES = ("img-src", "media-src", "connect-src")


def _csp_mode() -> str:
    mode = (os.environ.get(CSP_MODE_ENV) or "report").strip().lower()
    return mode if mode in _CSP_MODES else "report"


def load_console_csp(dist: Path) -> dict[str, list[str]] | None:
    """The directives in ``dist/csp.json``, or None when the build carries none (an older
    build): then no policy is sent — a guessed one could block the console."""
    try:
        doc = json.loads((dist / "csp.json").read_text(encoding="utf-8"))
        directives = doc["directives"]
        if isinstance(directives, dict) and all(isinstance(v, list) for v in directives.values()):
            return {str(k): [str(x) for x in v] for k, v in directives.items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _public_origins() -> list[str]:
    """This agent's own public origins: ``A2A_PUBLIC_URL`` and the declared public console URL."""
    from urllib.parse import urlsplit

    out: list[str] = []
    try:
        from infra.media import configured_public_url
        from security.public_console import public_origin

        for raw in (configured_public_url(), public_origin()):
            url = urlsplit(str(raw or ""))
            origin = f"{url.scheme}://{url.netloc}".lower() if url.scheme in ("http", "https") and url.netloc else ""
            if origin and origin not in out:
                out.append(origin)
    except Exception:  # noqa: BLE001 — a header is never worth failing the page over
        pass
    return out


def console_csp_headers(directives: dict[str, list[str]] | None) -> dict[str, str]:
    """The console page's headers. Framing is always limited to this origin (enforced even in
    report mode); the rest of the policy is reported or enforced per ``_csp_mode``."""
    headers = {"Content-Security-Policy": "frame-ancestors 'self'", "X-Frame-Options": "SAMEORIGIN"}
    mode = _csp_mode()
    if directives is None or mode == "off":
        return headers
    merged = {k: list(v) for k, v in directives.items()}
    for origin in _public_origins():
        for name in _PUBLIC_ORIGIN_DIRECTIVES:
            if name in merged and origin not in merged[name]:
                merged[name].append(origin)
    policy = "; ".join(" ".join([k, *v]) for k, v in merged.items())
    if mode == "enforce":
        headers["Content-Security-Policy"] = policy
    else:
        headers["Content-Security-Policy-Report-Only"] = f"{policy}; report-uri {CSP_REPORT_PATH}"
    return headers


# Reports are unauthenticated input: bound the body, keep a few named fields, strip anything
# that isn't printable, and log each distinct (directive, blocked) pair once per process.
_REPORT_MAX_BYTES = 16 * 1024
_REPORT_FIELDS = ("effective-directive", "violated-directive", "blocked-uri", "document-uri", "source-file", "line-number")
_REPORT_SEEN: set[tuple[str, str]] = set()
_REPORT_SEEN_CAP = 200
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")


def _clean(value: object, limit: int = 200) -> str:
    text = _UNPRINTABLE.sub("?", str(value if value is not None else ""))
    # A URL's query can carry a signed link or a token — keep the path, drop the rest.
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text[:limit]


def _report_entries(body: object) -> list[dict]:
    """The violation objects in either report shape: ``{"csp-report": {...}}`` (report-uri)
    or ``[{"type": "csp-violation", "body": {...}}]`` (the Reporting API)."""
    if isinstance(body, dict) and isinstance(body.get("csp-report"), dict):
        return [body["csp-report"]]
    if isinstance(body, list):
        out = []
        for item in body[:20]:
            inner = item.get("body") if isinstance(item, dict) else None
            if isinstance(inner, dict):
                # The Reporting API spells the fields in camelCase.
                out.append({re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), k): v for k, v in inner.items()})
        return out
    return []


def log_csp_report(raw: bytes) -> int:
    """Log what a report says would have been blocked; returns how many lines were logged."""
    if len(raw) > _REPORT_MAX_BYTES:
        return 0
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return 0
    logged = 0
    for entry in _report_entries(body):
        fields = {name: _clean(entry.get(name)) for name in _REPORT_FIELDS if entry.get(name) not in (None, "")}
        key = (fields.get("effective-directive") or fields.get("violated-directive", ""), fields.get("blocked-uri", ""))
        if key in _REPORT_SEEN or len(_REPORT_SEEN) >= _REPORT_SEEN_CAP:
            continue
        _REPORT_SEEN.add(key)
        log.warning("[console-csp] would block: %s", " ".join(f"{k}={v}" for k, v in fields.items()))
        logged += 1
    return logged


def mount_react_app(app, dist_dir: str | Path, *, app_path: str = "/app") -> bool:
    """Mount a built Vite app under ``app_path`` when dist assets exist.

    The console SPA (``/app`` + its assets) — console/full tiers only. The DS
    plugin-kit (``/_ds/…``) is mounted separately by ``mount_ds_plugin_kit`` so it
    rides every tier. Returns False when the build output is absent.
    """
    dist = Path(dist_dir).resolve()
    index_path = dist / "index.html"
    if not index_path.exists():
        return False

    app_path = "/" + app_path.strip("/")
    assets_dir = dist / "assets"
    if assets_dir.exists():
        app.mount(
            f"{app_path}/assets",
            StaticFiles(directory=str(assets_dir)),
            name="operator_assets",
        )

    # The console page may only be framed by its own origin: on an instance with no token an
    # outside page could otherwise overlay it and borrow the operator's clicks. Nothing of
    # ours frames it — member windows are routes of the same app, the desktop app loads its
    # bundle in its own windows, and plugin pages are framed BY the console, not the reverse.
    # The full policy rides beside it — reported by default, see ``console_csp_headers``. Built
    # per request so a live change to the public URL (Settings ▸ Network) applies at once.
    csp_directives = load_console_csp(dist)

    def page_headers() -> dict[str, str]:
        return console_csp_headers(csp_directives)

    @app.post(CSP_REPORT_PATH, include_in_schema=False)
    async def _csp_report(request: Request) -> Response:
        # Read at most one byte past the cap, so an oversized body is refused without buffering it.
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > _REPORT_MAX_BYTES:
                break
        log_csp_report(raw)
        return Response(status_code=204)

    @app.get(app_path, include_in_schema=False)
    async def _operator_index() -> FileResponse:
        return FileResponse(str(index_path), headers=page_headers())

    @app.get(f"{app_path}/{{path:path}}", include_in_schema=False)
    async def _operator_fallback(path: str) -> FileResponse:
        candidate = (dist / path).resolve()
        try:
            candidate.relative_to(dist)
        except ValueError:
            candidate = index_path
        if candidate.is_file():
            # Every file under the app path, not just the fallback: `/app/index.html` is the
            # same document as `/app`. The header means nothing on a script or an icon.
            return FileResponse(str(candidate), headers=page_headers())
        return FileResponse(str(index_path), headers=page_headers())

    return True
