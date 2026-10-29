"""Core access-log redaction (#4274 item 6): signed media links never reach the access log.

Every media link this server mints carries a replayable credential in its query — the core
``/media/?sig=…`` and expiring ``/media/?exp=…&sig=…`` (#4258), the fleet-proxied
``/agents/<slug>/media/?sig=…``, and the artifact ``/plugins/artifact/media/?t=…``. uvicorn logs
each request's full path verbatim, so the redactor (``infra.access_log_redact``, installed in
CORE startup) must scrub the credential out of any ``/media/`` path before the record is
formatted, while leaving non-media paths byte-for-byte unchanged. ``exp`` is a plain timestamp,
not a secret, so it stays.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from graph.fleet import proxy
from infra import access_log_redact

_UVICORN_FMT = '%s - "%s %s HTTP/%s" %d'  # uvicorn's AccessFormatter record shape


@pytest.fixture
def polling_logger(monkeypatch):
    lg = logging.getLogger("test.access_log_redaction.polling")
    monkeypatch.setattr(lg, "filters", [])
    monkeypatch.setattr(lg, "level", logging.INFO)
    access_log_redact.install(lg.name)
    access_log_redact.install(lg.name)
    assert len(lg.filters) == 2
    return lg


@pytest.mark.parametrize("path", access_log_redact._POLL_PATH_PREFIXES)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_successful_polls_are_debug(polling_logger, monkeypatch, path, method, level):
    monkeypatch.setattr(polling_logger, "level", level)
    rec = logging.LogRecord(
        polling_logger.name, logging.INFO, __file__, 1, _UVICORN_FMT,
        ("127.0.0.1:5", method, path + "?limit=10", "1.1", 200), None,
    )
    assert bool(polling_logger.filter(rec)) is (level == logging.DEBUG)
    assert (rec.levelno, rec.levelname) == (logging.DEBUG, "DEBUG")


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/api/runtime/status", 500),
    ("HEAD", "/healthz", 400),
    ("POST", "/api/runtime/status", 200),
    ("DELETE", "/api/background/job-1", 200),
    ("GET", "/api/chat", 200),
    ("GET", "/api/runtime/status-other", 200),
    ("GET", "/api/chat?next=/api/runtime/status", 200),
])
def test_other_access_records_stay_info(polling_logger, method, path, status):
    rec = logging.LogRecord(
        polling_logger.name, logging.INFO, __file__, 1, _UVICORN_FMT,
        ("127.0.0.1:5", method, path, "1.1", status), None,
    )
    assert polling_logger.filter(rec)
    assert (rec.levelno, rec.levelname) == (logging.INFO, "INFO")


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("path", ["/media/file", "/api/background/job/media/file"])
def test_installed_filters_redact_even_dropped_polls(polling_logger, monkeypatch, level, path):
    monkeypatch.setattr(polling_logger, "level", level)
    rec = logging.LogRecord(
        polling_logger.name, logging.INFO, __file__, 1, _UVICORN_FMT,
        ("127.0.0.1:5", "GET", path + "?sig=secret", "1.1", 200), None,
    )
    kept = polling_logger.filter(rec)
    assert bool(kept) is (path.startswith("/media/") or level == logging.DEBUG)
    assert "secret" not in rec.getMessage()
    assert "sig=…" in rec.getMessage()


def test_poll_filter_preserves_preformatted_records(polling_logger):
    rec = logging.LogRecord(
        polling_logger.name, logging.INFO, __file__, 1,
        "GET /media/file?sig=secret 200", (), None,
    )
    assert polling_logger.filter(rec)
    assert rec.levelno == logging.INFO
    assert "secret" not in rec.getMessage()


def _format_access(path: str, status: int = 200) -> str:
    """One uvicorn-style access record run through the core redact filter, then formatted — the
    exact sequence the ``uvicorn.access`` logger performs when the filter is attached."""
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, _UVICORN_FMT,
        ("127.0.0.1:5", "GET", path, "1.1", status), None,
    )
    assert access_log_redact.MediaRedactFilter().filter(rec) is True
    return rec.getMessage()


# ── the credential never survives to the formatted line (r1, r2) ──────────────


def test_core_media_signature_is_redacted():
    sig = "a" * 64
    line = _format_access(f"/media/abc.png?sig={sig}")
    assert sig not in line  # no part of the signature survives
    assert 'GET /media/abc.png?sig=… HTTP/1.1' in line


def test_expiring_and_fleet_media_signatures_are_redacted():
    sig = "b" * 64
    expiring = _format_access(f"/media/abc.png?exp=1700000000&sig={sig}")
    assert sig not in expiring and "sig=…" in expiring
    assert "exp=1700000000" in expiring  # exp is not secret — it stays

    fleet = _format_access(f"/agents/foo/media/abc.png?exp=1&sig={sig}")
    assert sig not in fleet and "sig=…" in fleet
    assert "/agents/foo/media/abc.png" in fleet and "exp=1" in fleet


def test_artifact_media_ticket_still_redacted():
    """The shape the artifact plugin mints (and its existing test guards) — one /media/ path, the
    t ticket param — is redacted by the same core filter."""
    tok = "SECRETtok-_123"
    line = _format_access(f"/plugins/artifact/media/a-1?version=1&t={tok}")
    assert tok not in line and "t=…" in line and "version=1" in line


# ── scope: only media paths, and nothing else is touched (r4) ─────────────────


def test_non_media_path_is_not_rewritten():
    # /api/chat carries a token param but is NOT a media path → left byte-for-byte, token and all
    line = _format_access("/api/chat?token=super-secret")
    assert line == '127.0.0.1:5 - "GET /api/chat?token=super-secret HTTP/1.1" 200'


def test_media_path_without_query_is_untouched():
    line = _format_access("/media/abc.png")
    assert line == '127.0.0.1:5 - "GET /media/abc.png HTTP/1.1" 200'


def test_redact_is_byte_for_byte_on_non_media_and_covers_every_param():
    # every credential param in a media path is scrubbed; exp and unrelated params stay
    got = access_log_redact.redact("/media/a.png?exp=1&sig=deadbeef&t=tok&ticket=x&token=y&v=2")
    assert got == "/media/a.png?exp=1&sig=…&t=…&ticket=…&token=…&v=2"
    # fleet + artifact shapes
    assert access_log_redact.redact("/agents/m/media/a.png?sig=abc") == "/agents/m/media/a.png?sig=…"
    # non-media strings return identical objects (byte-for-byte), even carrying a token
    for s in ("/api/chat?token=x", "/healthz", "", "no query at all"):
        assert access_log_redact.redact(s) == s


# ── the filter handles both record shapes uvicorn can produce ─────────────────


def test_filter_handles_args_tuple_and_preformatted_msg():
    sig = "f" * 64
    # args-tuple branch (uvicorn's normal %-style record)
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, _UVICORN_FMT,
        ("1.2.3.4:5", "GET", f"/media/a.png?sig={sig}", "1.1", 200), None,
    )
    assert access_log_redact.MediaRedactFilter().filter(rec) is True
    assert sig not in rec.getMessage() and "sig=…" in rec.getMessage()

    # pre-formatted msg branch (no args)
    rec2 = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, f"GET /media/a.png?sig={sig} 200", None, None,
    )
    assert access_log_redact.MediaRedactFilter().filter(rec2) is True
    assert sig not in rec2.getMessage() and "sig=…" in rec2.getMessage()


# ── install: idempotent, and the core server path needs no plugin (r5, r6) ────


def test_install_twice_attaches_exactly_one_filter():
    name = "test.access_log_redaction.idempotent"
    lg = logging.getLogger(name)
    lg.filters = []
    access_log_redact.install(name)
    access_log_redact.install(name)
    mine = [f for f in lg.filters if isinstance(f, access_log_redact.MediaRedactFilter)]
    assert len(mine) == 1


def test_core_server_install_path_needs_no_plugin():
    """The function core server startup calls (``install()`` with its default target) attaches the
    redactor to ``uvicorn.access`` with no dependency on the artifact plugin being loaded."""
    lg = logging.getLogger("uvicorn.access")
    before = list(lg.filters)
    try:
        lg.filters = [f for f in before if not isinstance(f, access_log_redact.MediaRedactFilter)]
        access_log_redact.install()  # exactly as src/server/__init__.py calls it
        mine = [f for f in lg.filters if isinstance(f, access_log_redact.MediaRedactFilter)]
        assert len(mine) == 1
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "%s", (f"/media/x.png?sig={'c' * 64}",), None,
        )
        assert mine[0].filter(rec) is True
        assert "sig=…" in rec.getMessage() and "c" * 64 not in rec.getMessage()
    finally:
        lg.filters = before


def test_media_redaction_survives_uvicorn_config_logging_init():
    """server/__init__ installs the redaction filter, THEN constructs a ``uvicorn.Config`` — whose
    ``__init__`` runs its own ``dictConfig(LOGGING_CONFIG)`` and reconfigures the ``uvicorn.access``
    logger (dropping and rebuilding its handlers). The filter lives on the LOGGER and must survive
    that reconfigure, or every signed media link hits the access log unredacted the moment the
    server boots (r2). This locks the install-order + survival in regardless of the stdlib version's
    dictConfig behavior."""
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    lg = logging.getLogger("uvicorn.access")
    saved = list(lg.filters)
    try:
        # Clean slate, then install exactly as core startup does (before the Config is built).
        lg.filters = [f for f in saved if not isinstance(f, access_log_redact.MediaRedactFilter)]
        access_log_redact.install()

        async def _app(scope, receive, send):  # minimal ASGI app — the Config is built, never served
            raise AssertionError("not served in this test")

        uvicorn.Config(_app, log_config=LOGGING_CONFIG)  # __init__ applies the logging dictConfig

        mine = [f for f in lg.filters if isinstance(f, access_log_redact.MediaRedactFilter)]
        assert len(mine) == 1, "uvicorn.Config logging init dropped the media redaction filter"

        sig = "e" * 64
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "%s", (f"/media/x.png?sig={sig}",), None,
        )
        assert mine[0].filter(rec) is True
        assert "sig=…" in rec.getMessage() and sig not in rec.getMessage()
    finally:
        lg.filters = saved


# ── the fleet proxy's own timeout/error logs don't leak a signed upstream URL (#4446 item 1) ──
# Beyond the access log, the hub PROXIES signed media (``/agents/<slug>/media/?sig=…``). Both
# upstream proxy log paths — ``_forward_to_base`` (the 502/504 + mid-stream stall) and
# ``_forward_chat_detach`` (the submit timeout/error) — run their URL through the SAME core
# ``redact`` before logging, so a failure can't print a replayable ``sig``. No real signed URL or
# credential appears in these fixtures.


class _FakeRequest:
    def __init__(self, *, method="GET", headers=None, query=None, body=b""):
        self.method = method
        self.headers = headers or {}
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return self._body


class _RaisingClient:
    """An httpx-like client whose send() always raises — to drive a proxy timeout/error log path."""

    def __init__(self, exc):
        self._exc = exc

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, *args, **kwargs):
        raise self._exc


async def test_forward_to_base_timeout_redacts_signed_media_url(monkeypatch, caplog):
    monkeypatch.setattr(proxy, "_get_client", lambda: _RaisingClient(httpx.ReadTimeout("stalled")))
    sig = "a" * 64
    # A signed fleet-media sub-path carried into the proxied URL — the 504 timeout log must redact it.
    path = f"media/report.png?exp=1700000000&sig={sig}"
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        resp = await proxy._forward_to_base("http://127.0.0.1:7001", _FakeRequest(), path)
    assert resp.status_code == 504
    assert sig not in caplog.text and "sig=…" in caplog.text
    assert "exp=1700000000" in caplog.text  # the non-secret timestamp stays — diagnostics preserved


async def test_forward_to_base_timeout_never_logs_the_sig_from_query_params(monkeypatch, caplog):
    """In the REAL fleet-media flow the sig rides in the query string (forwarded to httpx via
    ``params=``), not the sub-path — so the proxy's own URL log never even contains it."""
    monkeypatch.setattr(proxy, "_get_client", lambda: _RaisingClient(httpx.ReadTimeout("stalled")))
    sig = "b" * 64
    req = _FakeRequest(query={"exp": "1700000000", "sig": sig})
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        resp = await proxy._forward_to_base("http://127.0.0.1:7001", req, "media/report.png")
    assert resp.status_code == 504
    assert sig not in caplog.text


async def test_detached_submit_timeout_redacts_url_and_never_logs_the_body(monkeypatch, caplog):
    monkeypatch.setattr(proxy, "_get_client", lambda: _RaisingClient(httpx.ReadTimeout("stalled")))
    sig = "c" * 64
    body = b'{"message": "user secret prompt"}'
    path = f"media/report.png?exp=1&sig={sig}"
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        resp = await proxy._forward_chat_detach(
            "ava-1a2b", "http://127.0.0.1:7001", _FakeRequest(method="POST", body=body),
            path, {}, frozenset(), body,
        )
    assert resp.status_code == 504
    assert sig not in caplog.text and "sig=…" in caplog.text
    assert "user secret prompt" not in caplog.text  # the request body is never logged


async def test_detached_submit_error_redacts_signed_media_url(monkeypatch, caplog):
    # A non-timeout transport failure on submit takes the 502 branch — it must redact the URL too.
    monkeypatch.setattr(proxy, "_get_client", lambda: _RaisingClient(httpx.RemoteProtocolError("boom")))
    sig = "d" * 64
    path = f"media/report.png?sig={sig}"
    with caplog.at_level(logging.WARNING, logger="protoagent.server"):
        resp = await proxy._forward_chat_detach(
            "ava-1a2b", "http://127.0.0.1:7001", _FakeRequest(method="POST"), path, {}, frozenset(), b"{}",
        )
    assert resp.status_code == 502
    assert sig not in caplog.text and "sig=…" in caplog.text
