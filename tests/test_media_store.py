"""Tests for the core media output channel (#1929).

Covers the three layers:
  - the store (``infra/media.py``): save (bytes + source path), instance-dir
    placement, signed URLs, name validation, retention GC;
  - the registry helper (``PluginRegistry.save_media``): provenance meta +
    the ``media.saved`` bus event;
  - the serving side: the ``server/media.py`` route (content + 404 posture)
    and the auth middleware (``a2a_impl/auth.py``) — gated by default (signed
    URL or bearer required), ``media.public`` is the explicit opt-in.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from infra import media


@pytest.fixture(autouse=True)
def _isolated_instance(tmp_path, monkeypatch):
    """Point the instance root at a per-test temp dir so the store never touches
    the developer's real ``~/.protoagent`` (paths re-resolve via the conftest
    autouse ``_reset_instance_paths`` fixture)."""
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    yield


def _set_media_config(monkeypatch, *, public: bool = False, retention_days: int = 0):
    """Install a live server config carrying the media policy knobs."""
    from runtime.state import STATE

    monkeypatch.setattr(
        STATE, "graph_config", SimpleNamespace(media_public=public, media_retention_days=retention_days)
    )


# ── the store ─────────────────────────────────────────────────────────────────


def test_save_bytes_lands_under_instance_media_dir(tmp_path):
    ref = media.save_media(b"\x89PNG-not-really", "image/png", {"prompt": "a red square"})
    assert ref.path.is_file()
    assert ref.path.parent == tmp_path / "instance" / "media"  # instance_paths().store("media")
    assert ref.path.suffix == ".png"
    assert ref.path.read_bytes() == b"\x89PNG-not-really"
    assert ref.mime == "image/png"
    # URL shape: /media/<file>?sig=<hmac> — servable + embeddable in markdown.
    assert ref.url.startswith(f"/media/{ref.path.name}?sig=")
    # Provenance sidecar is a HIDDEN file (never served) carrying mime + meta.
    sidecar = ref.path.parent / f".{ref.id}.json"
    data = json.loads(sidecar.read_text())
    assert data["mime"] == "image/png"
    assert data["meta"]["prompt"] == "a red square"


def test_save_from_source_path_copies_the_file(tmp_path):
    src = tmp_path / "generated.jpg"
    src.write_bytes(b"jpeg-bytes")
    ref = media.save_media(src, "image/jpeg")
    assert ref.path.suffix == ".jpg"
    assert ref.path.read_bytes() == b"jpeg-bytes"
    assert src.is_file()  # copied, not moved


def test_save_missing_source_raises():
    with pytest.raises(FileNotFoundError):
        media.save_media("/nonexistent/never.png", "image/png")


def test_unknown_mime_falls_back_to_bin():
    ref = media.save_media(b"??", "application/x-protoagent-mystery")
    assert ref.path.suffix == ".bin"


def test_signature_verifies_and_rejects_tampering():
    name = "abc123.png"
    sig = media.sign_name(name)
    assert media.verify_name(name, sig)
    assert not media.verify_name(name, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert not media.verify_name("other.png", sig)
    assert not media.verify_name(name, "")


def test_signing_key_is_persistent_and_private(tmp_path):
    sig1 = media.sign_name("x.png")
    keyfile = tmp_path / "instance" / "media" / ".signing-key"
    assert keyfile.is_file()
    assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    assert media.sign_name("x.png") == sig1  # stable across calls (same key)


def test_request_allowed_gated_by_default():
    ref = media.save_media(b"png", "image/png")
    name = ref.path.name
    assert media.request_allowed(f"/media/{name}", media.sign_name(name))
    assert not media.request_allowed(f"/media/{name}", "")  # unsigned → deny
    assert not media.request_allowed(f"/media/{name}", "bogus")
    # Store internals and traversal shapes are never exempt, signed or not.
    assert not media.request_allowed("/media/.signing-key", media.sign_name(".signing-key"))
    assert not media.request_allowed("/media/../secrets.yaml", media.sign_name("../secrets.yaml"))
    assert not media.request_allowed("/api/chat", "x")  # not a media path at all


def test_request_allowed_public_opt_in(monkeypatch):
    _set_media_config(monkeypatch, public=True)
    assert media.request_allowed("/media/anything.png", "")
    # ... but internals stay unreachable even when public.
    assert not media.request_allowed("/media/.signing-key", "")


def test_resolve_media_returns_path_and_sidecar_mime():
    ref = media.save_media(b"webp-bytes", "image/webp")
    got = media.resolve_media(ref.path.name)
    assert got is not None
    path, mime = got
    assert path == ref.path
    assert mime == "image/webp"


def test_resolve_media_guesses_mime_without_sidecar(tmp_path):
    d = media.media_dir(create=True)
    (d / "orphan.png").write_bytes(b"png")
    got = media.resolve_media("orphan.png")
    assert got is not None
    assert got[1] == "image/png"


def test_resolve_media_rejects_unsafe_and_absent_names():
    media.media_dir(create=True)
    assert media.resolve_media("missing.png") is None
    assert media.resolve_media(".signing-key") is None
    assert media.resolve_media("../secrets.yaml") is None
    assert media.resolve_media("a/b.png") is None
    assert media.resolve_media("") is None


def test_retention_gc_prunes_old_files_on_save(monkeypatch):
    _set_media_config(monkeypatch, retention_days=7)
    old = media.save_media(b"old", "image/png")
    old_sidecar = old.path.parent / f".{old.id}.json"
    stale = time.time() - 8 * 86400
    os.utime(old.path, (stale, stale))

    fresh = media.save_media(b"fresh", "image/png")  # save triggers the sweep

    assert not old.path.exists()
    assert not old_sidecar.exists()
    assert fresh.path.exists()
    assert (fresh.path.parent / ".signing-key").exists()  # internals exempt from GC


def test_retention_zero_keeps_everything(monkeypatch):
    _set_media_config(monkeypatch, retention_days=0)
    old = media.save_media(b"old", "image/png")
    stale = time.time() - 365 * 86400
    os.utime(old.path, (stale, stale))
    media.save_media(b"fresh", "image/png")
    assert old.path.exists()


# ── the registry helper ───────────────────────────────────────────────────────


def test_registry_save_media_records_plugin_and_emits_event(tmp_path, monkeypatch):
    from graph.plugins.host import HOST
    from graph.plugins.registry import PluginRegistry

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(HOST, "publish", lambda topic, data: events.append((topic, data)))
    reg = PluginRegistry("imagegen", tmp_path)

    ref = reg.save_media(b"png-bytes", "image/png", {"prompt": "sunset"})

    assert ref.path.is_file()
    sidecar = json.loads((ref.path.parent / f".{ref.id}.json").read_text())
    assert sidecar["meta"]["plugin_id"] == "imagegen"  # provenance auto-recorded
    assert sidecar["meta"]["prompt"] == "sunset"
    assert events == [("media.saved", {"id": ref.id, "url": ref.url, "mime": "image/png", "plugin": "imagegen"})]


def test_registry_save_media_without_bus_still_saves(tmp_path, monkeypatch):
    from graph.plugins.host import HOST
    from graph.plugins.registry import PluginRegistry

    monkeypatch.setattr(HOST, "publish", None)  # headless / test context — no bus wired
    ref = PluginRegistry("imagegen", tmp_path).save_media(b"png", "image/png")
    assert ref.path.is_file()


# ── the serving route (server/media.py) ───────────────────────────────────────


def _route_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.media import register_media_routes

    app = FastAPI()
    register_media_routes(app)
    return TestClient(app)


def test_route_serves_saved_file_with_mime():
    ref = media.save_media(b"png-payload", "image/png")
    c = _route_client()
    r = c.get(f"/media/{ref.path.name}")
    assert r.status_code == 200
    assert r.content == b"png-payload"
    assert r.headers["content-type"] == "image/png"


def test_route_survives_restart_simulation():
    # The URL is derived from on-disk state only — a fresh app (new process)
    # serves files saved before it existed.
    ref = media.save_media(b"persistent", "image/png")
    assert _route_client().get(f"/media/{ref.path.name}").status_code == 200


def test_route_404s_absent_and_internal_names():
    media.save_media(b"png", "image/png")  # store exists, with a signing key in it
    c = _route_client()
    assert c.get("/media/missing.png").status_code == 404
    assert c.get("/media/.signing-key").status_code == 404
    assert c.get("/media/%2e%2e/secrets.yaml").status_code == 404


# ── the auth gate (a2a_impl/auth.py) ──────────────────────────────────────────


@pytest.fixture()
def _bearer_guard():
    """Seed the default-deny guard with a bearer token; reset module state after
    (mirrors tests/test_a2a_auth.py's fixture)."""
    from a2a_impl import auth

    auth.configure(bearer_token="secret-token", api_key="", allowed_origins_raw="")
    yield auth
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


def _gated_client(auth_mod):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[Route("/media/{name}", lambda r: PlainTextResponse("served"), methods=["GET"])])
    app.add_middleware(auth_mod.A2AAuthMiddleware)
    return TestClient(app)


def test_gate_denies_unsigned_media_by_default(_bearer_guard):
    media.media_dir(create=True)
    c = _gated_client(_bearer_guard)
    assert c.get("/media/f.png").status_code == 401
    assert c.get("/media/f.png?sig=bogus").status_code == 401


def test_gate_admits_signed_url_without_bearer(_bearer_guard):
    ref = media.save_media(b"png", "image/png")
    c = _gated_client(_bearer_guard)
    assert c.get(ref.url).status_code == 200  # the exact URL save_media returned


def test_gate_still_accepts_bearer_header(_bearer_guard):
    media.media_dir(create=True)
    c = _gated_client(_bearer_guard)
    r = c.get("/media/f.png", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200


def test_gate_public_opt_in_admits_unsigned(_bearer_guard, monkeypatch):
    _set_media_config(monkeypatch, public=True)
    c = _gated_client(_bearer_guard)
    assert c.get("/media/f.png").status_code == 200
    # Internals never become public — falls through to bearer, which is absent.
    assert c.get("/media/.signing-key").status_code == 401
