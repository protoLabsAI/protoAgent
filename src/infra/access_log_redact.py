"""Redact signed media links out of the uvicorn access log (#4274 item 6).

Every media link this server mints rides a credential in its query string, and uvicorn's
access logger writes each request's full path verbatim — so a log reader could *replay* a
live link. The core store (``infra/media.py``) mints ``/media/<name>?sig=…`` and the expiring
``/media/<name>?exp=<unix>&sig=…`` (#4258), the fleet proxy forwards the member form
``/agents/<slug>/media/<file>?exp=…&sig=…`` (``a2a_impl/auth.py``), and the artifact plugin
adds ``/plugins/artifact/media/<id>?version=…&t=<ticket>``. All three share the ``/media/``
path segment.

This is the ONE redactor for all of them: a :class:`logging.Filter` on ``uvicorn.access``
that rewrites the value of the credential-bearing query params (``sig``, ``t``, ``ticket``,
``token``) to ``…`` in any request path containing ``/media/``, before the record is
formatted. ``exp`` is a plain unix timestamp, not a secret, so it is left intact. Non-media
paths are left byte-for-byte unchanged, so a ``?token=`` on some other route is untouched —
redaction is scoped to media paths on purpose.

Core server startup installs it (``server/__init__.py``) before uvicorn serves, so it runs
regardless of which plugins are enabled; the artifact plugin keeps a thin shim that delegates
here so there is a single implementation.
"""

from __future__ import annotations

import logging

import re

# Credential-bearing query params on a media link. ``sig`` is the core HMAC signature (and the
# ``exp``-paired one on expiring links); ``t``/``ticket``/``token`` are the artifact media
# ticket's aliases. The trailing ``=`` anchor keeps ``token`` from being shortened to ``t``.
_REDACT_PARAMS = re.compile(r"([?&](?:sig|t|ticket|token)=)[^&#\s\"]*")
# Only touch paths that name a media route. ``/media/`` is a substring of all three link shapes
# (``/media/``, ``/agents/<slug>/media/``, ``/plugins/artifact/media/``), so one marker covers them.
_MEDIA_MARK = "/media/"


def redact(text: str) -> str:
    """``text`` with every credential query value in a media-route URL replaced by ``…``.

    A non-media string (no ``/media/`` segment) is returned unchanged."""
    if _MEDIA_MARK not in text:
        return text
    return _REDACT_PARAMS.sub(r"\1…", text)


class MediaRedactFilter(logging.Filter):
    """Redacts media-link credentials from access-log records — both the ``%s``-style
    ``record.args`` tuple uvicorn logs with AND a pre-formatted ``record.msg``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(isinstance(a, str) and _MEDIA_MARK in a for a in record.args):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str) and _MEDIA_MARK in record.msg:
            record.msg = redact(record.msg)
        return True


def install(logger_name: str = "uvicorn.access", *, filter_cls: type[logging.Filter] = MediaRedactFilter) -> None:
    """Attach ``filter_cls`` to ``logger_name`` once (idempotent — never a second copy).

    Idempotency keys on the filter class NAME, so re-running install (or an artifact-plugin
    router rebuild that installs its back-compat subclass) never stacks duplicates."""
    lg = logging.getLogger(logger_name)
    name = filter_cls.__name__
    if not any(type(f).__name__ == name for f in lg.filters):
        lg.addFilter(filter_cls())
