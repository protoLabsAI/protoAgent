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

from infra import access_log_redact

_UVICORN_FMT = '%s - "%s %s HTTP/%s" %d'  # uvicorn's AccessFormatter record shape


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
