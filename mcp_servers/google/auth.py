"""OAuth2 + service construction for the Google MCP server (Slice 2 / ADR 0017).

Installed-app ("Desktop app") OAuth. The operator's OAuth client
(``client_id`` + ``client_secret``) and the cached, refreshable token now come
from the **in-app config** (``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` env,
injected by the server; ``GOOGLE_TOKEN_PATH`` in the per-user config dir) rather
than ``credentials.json`` / ``token.json`` files — so it works in the bundled
desktop app with no CLI step. The legacy file path is kept as a fallback.

"Connect Google" in the UI calls :func:`run_consent` (opens the browser, runs the
loopback consent, caches the token). The MCP subprocess calls
:func:`build_services` (loads/refreshes the cached token). Thin wrapper around
``google-auth*`` — not unit-tested (the testable logic is in ``gmail.py`` /
``calendar.py``).

Scopes (least privilege for v1): Gmail **readonly** + **compose** (drafts only,
never send) and Calendar **readonly**.
"""

from __future__ import annotations

import os
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_HERE = Path(__file__).resolve().parent


def _path(env: str, default_name: str) -> Path:
    return Path(os.environ.get(env) or (_HERE / default_name))


def token_path() -> Path:
    """Where the refreshable token is cached (config dir via env, else local)."""
    return _path("GOOGLE_TOKEN_PATH", "token.json")


def _client_config() -> dict | None:
    """Build an installed-app client config from env, or None if unset.

    Mirrors the shape of a Desktop-app ``credentials.json`` so we can use
    ``InstalledAppFlow.from_client_config`` without a file on disk.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _load_cached():
    """Load + refresh the cached token; return valid Credentials or None."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tp = token_path()
    if not tp.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tp.write_text(creds.to_json())
        return creds
    return None


def run_consent() -> str:
    """Run the interactive OAuth consent (opens a browser, loopback redirect),
    cache the token, and return the connected account's email.

    Blocking — the caller should run it off the event loop. Requires the OAuth
    client in the env (``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = _client_config()
    if not client_config:
        raise ValueError(
            "Google OAuth client not configured — set the OAuth client ID + secret "
            "(System → Settings → Google) before connecting."
        )
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    tp = token_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(creds.to_json())
    return _email_for(creds)


def _email_for(creds) -> str:
    """Best-effort connected-account email (via the Gmail profile)."""
    try:
        from googleapiclient.discovery import build

        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return gmail.users().getProfile(userId="me").execute().get("emailAddress", "")
    except Exception:  # noqa: BLE001 — status is best-effort
        return ""


def connection_status() -> dict:
    """Report (configured, connected, email) for the UI without forcing consent."""
    configured = _client_config() is not None or _path("GOOGLE_CREDENTIALS_PATH", "credentials.json").exists()
    creds = None
    try:
        creds = _load_cached()
    except Exception:  # noqa: BLE001
        creds = None
    return {
        "configured": configured,
        "connected": bool(creds and creds.valid),
        "email": _email_for(creds) if creds and creds.valid else None,
    }


def get_credentials():
    """Return valid Google credentials, refreshing or running consent as needed.

    Prefers the cached token; if absent and an OAuth client is configured (env or
    legacy ``credentials.json``), runs the consent flow. The desktop app primes
    the token via :func:`run_consent` first, so the MCP subprocess just loads it.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = _load_cached()
    if creds:
        return creds

    client_config = _client_config()
    if client_config:
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    else:
        creds_path = _path("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        if not creds_path.exists():
            raise FileNotFoundError(
                "Google OAuth client not configured. Set the OAuth client ID + secret "
                "in System → Settings → Google (or provide a credentials.json)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)

    creds = flow.run_local_server(port=0, open_browser=True)
    tp = token_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(creds.to_json())
    return creds


def build_services():
    """Build authorized ``(gmail, calendar)`` service resources.

    **Load-only** — never runs interactive consent. The MCP server runs headless
    (it's a subprocess), so it must not pop a browser; the operator authorizes
    once via "Connect Google" (:func:`run_consent`). If there's no cached token,
    raise a clear error so the tool fails cleanly instead of hanging.
    """
    from googleapiclient.discovery import build

    creds = _load_cached()
    if not creds:
        raise RuntimeError(
            "Google not connected — open System → Settings → Google and click “Connect Google” to authorize."
        )
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return gmail, calendar
