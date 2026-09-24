"""Where the shim finds the instance URL and bearer token.

Precedence: explicit flags → environment (``PROTOAGENT_URL`` / ``PROTOAGENT_TOKEN`` /
``PROTOAGENT_TOKEN_FILE``) → the stored credentials file written by
``protoagent-acp login`` (the ACP *terminal* auth method: Zed runs that command in a
terminal, the operator pastes a token once, and every later launch finds it here).
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:7870"


def credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "protoagent-acp" / "credentials.json"


@dataclass
class Credentials:
    url: str
    token: str | None
    source: str


def _read_token_file(path: str) -> str | None:
    try:
        token = Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _stored() -> dict:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve(url: str | None = None, token: str | None = None, token_file: str | None = None) -> Credentials:
    stored = _stored()
    resolved_url = url or os.environ.get("PROTOAGENT_URL") or stored.get("url") or DEFAULT_URL
    if token:
        return Credentials(resolved_url, token, "--token")
    if token_file:
        return Credentials(resolved_url, _read_token_file(token_file), f"--token-file {token_file}")
    if os.environ.get("PROTOAGENT_TOKEN"):
        return Credentials(resolved_url, os.environ["PROTOAGENT_TOKEN"].strip(), "PROTOAGENT_TOKEN")
    if os.environ.get("PROTOAGENT_TOKEN_FILE"):
        f = os.environ["PROTOAGENT_TOKEN_FILE"]
        return Credentials(resolved_url, _read_token_file(f), f"PROTOAGENT_TOKEN_FILE {f}")
    # A stored token only applies to the URL it was saved for — never send one
    # instance's credential to another.
    if stored.get("token") and stored.get("url", resolved_url).rstrip("/") == resolved_url.rstrip("/"):
        return Credentials(resolved_url, str(stored["token"]), str(credentials_path()))
    return Credentials(resolved_url, None, "none")


def login(url: str | None = None) -> int:
    """Interactive: ask for URL + token, store them 0600. Runs in a TERMINAL (Zed's
    terminal-auth flow, or by hand) — so stdout is a human's here, not ACP."""
    default = url or _stored().get("url") or DEFAULT_URL
    try:
        entered = input(f"protoAgent URL [{default}]: ").strip() or default
        token = getpass.getpass("Bearer token (the instance's auth token; blank for none): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 1
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"url": entered.rstrip("/"), "token": token or None}, fh)
    print(f"saved to {path}")
    return 0
