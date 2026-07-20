"""Fleet service token (ADR 0089) — the instance's internal, loopback-only credential.

The hub is the single auth boundary for an instance (ADR 0089 D1). A member sits *behind*
the hub's reverse proxy (ADR 0042) and must not re-authenticate the external caller — each
member has its own ``instance_root`` (ADR 0065), hence its own ``devices.json`` (ADR 0087)
and ``auth.token``, so a hub-minted device token can never verify against a sister. Instead
the hub presents members with **this** token, which every agent in the fleet accepts as the
``operator`` tier.

Lifecycle:

* **Generated once**, persisted ``0600`` at ``workspaces_root()/.fleet-token`` — hub-instance
  scoped, beside the ``fleet.json`` registry the hub already owns (``graph/fleet/supervisor``).
* **Delivered to members by env** at spawn (``PROTOAGENT_FLEET_TOKEN``), never by config or
  URL — the same discipline the desktop self-auth used (#2055): a service credential must not
  ride a file that ``workspace new --from`` copies, nor anything the page can read. A member's
  own ``workspaces_root`` is empty by construction, so it never reads the file — it reads the
  injected env var.

``resolve_service_token()`` is the single entry point for both sides: the env var wins (a
member), else read-or-create the file (a hub / standalone instance). Process-cached because
the value is constant for the process; rotation is restart-gated (ADR 0089 D6).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_VAR = "PROTOAGENT_FLEET_TOKEN"
_FILENAME = ".fleet-token"
_TOKEN_BYTES = 32  # 256 bits, url-safe — a machine-to-machine secret, never typed.

# Process cache — the token is constant for a process; rotation requires a restart.
_cached: list[str | None] = [None]


def _token_path() -> Path:
    from graph.workspaces import manager

    return manager.workspaces_root() / _FILENAME


def _read_or_create() -> str:
    """Return the persisted fleet token, minting + writing one atomically if absent.

    Tolerant of a concurrent creator (hub boot vs the fleet CLI vs the supervisor): a
    pid-unique temp file + atomic ``os.replace`` means a race can't truncate the token, and
    a re-read after the write yields whichever creator won — so every caller in the instance
    converges on one value.
    """
    path = _token_path()
    try:
        existing = path.read_text("utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("[fleet] fleet-token unreadable at %s — regenerating", path)

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{_FILENAME}.{os.getpid()}.tmp")
        tmp.write_text(token, "utf-8")
        os.chmod(tmp, 0o600)  # 0600 before the rename — a service credential, even on loopback.
        os.replace(tmp, path)  # atomic
        # Re-read: a concurrent creator may have won the replace race.
        winner = path.read_text("utf-8").strip()
        return winner or token
    except OSError:
        # Read-only / undeletable instance root: fall back to a process-lifetime token so the
        # instance still runs. A restart mints a fresh one, so members spawned before the
        # restart would mismatch — logged, but never an auth outage on the shared bearer.
        logger.warning("[fleet] could not persist fleet-token at %s — using an ephemeral one", path)
        return token


def resolve_service_token() -> str:
    """The fleet service token for THIS process (loopback-only; never log it).

    Env wins — a member reads the value the hub injected at spawn. A hub / standalone
    instance reads-or-creates the persisted file. Cached for the process.
    """
    if _cached[0]:
        return _cached[0]
    env = os.environ.get(ENV_VAR, "").strip()
    token = env or _read_or_create()
    _cached[0] = token
    return token
