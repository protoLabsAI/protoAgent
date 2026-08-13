"""Published-link registry (#2179 P2, #2684) — what this instance has published to the
hosted viewer, and the revoke token each needs to be un-shared.

Mirrors ``security/devices.py``'s shape (one JSON file at ``instance_root``, atomic
write — both hold a live credential) with one deliberate divergence: devices.py NEVER
stores a token, only its hash, because a device token only ever needs to be *verified*
locally. A revoke token has the opposite requirement — this instance must *present* it to
the hosted service's revoke endpoint later, and a hash can't be reversed back into the
original value. So the token is stored in plaintext here, same as any other locally-held
API credential (mirroring how ``secrets.yaml`` holds real values, not hashes), and
``list_published_links()`` (the shape sent to the browser) never includes it — only
``get_link()`` (server-internal, used to actually call the hosted service) does.

Because the plaintext token makes this a genuinely sensitive file (unlike devices.json's
hashes, "aren't replayable" doesn't apply here), it uses the full portable owner-only
contract (``infra.paths.harden_private_file``, #2412 phase 4) rather than a bare
``os.chmod`` — POSIX mode bits are decorative on Windows (``stat`` reports 0666
regardless), so a raw ``chmod(0o600)`` alone leaves the file NOT actually restricted
there; see ``tests/privacy_asserts.assert_owner_only`` for how this gets verified
cross-platform.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from infra.paths import harden_private_file, instance_paths

logger = logging.getLogger(__name__)


@dataclass
class PublishedLink:
    id: str
    thread_id: str
    title: str
    public_url: str
    revoke_token: str
    published_at: float
    expires_at: str | None = None
    revoked_at: float | None = None

    def public(self) -> dict:
        """The shape safe to hand the console — everything except the revoke token."""
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "title": self.title,
            "public_url": self.public_url,
            "published_at": self.published_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }


def _registry_path() -> Path:
    return instance_paths().instance_root / "published_links.json"


def _load() -> list[PublishedLink]:
    path = _registry_path()
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # A corrupt registry must not break publishing — treat it as empty. The instance
        # loses its local record of past links (it can't revoke them from here anymore),
        # but nothing about the hosted side changes, and new publishes still work.
        logger.warning("[publish] registry unreadable at %s — treating as empty", path)
        return []
    out: list[PublishedLink] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(
                PublishedLink(
                    id=str(item["id"]),
                    thread_id=str(item["thread_id"]),
                    title=str(item.get("title") or ""),
                    public_url=str(item["public_url"]),
                    revoke_token=str(item.get("revoke_token") or ""),
                    published_at=float(item["published_at"]),
                    expires_at=(str(item["expires_at"]) if item.get("expires_at") else None),
                    revoked_at=(float(item["revoked_at"]) if item.get("revoked_at") else None),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # skip a hand-edited/partial entry rather than failing the whole load
    return out


def _save(links: list[PublishedLink]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([asdict(link) for link in links], indent=2), "utf-8")
    os.chmod(tmp, 0o600)  # POSIX belt on the tmp file, before the rename
    os.replace(tmp, path)  # atomic — a crash mid-write can't truncate the registry
    harden_private_file(path)  # the Windows ACL belt, applied to the final path post-rename


def record_publish(
    *, thread_id: str, title: str, public_url: str, revoke_token: str, expires_at: str | None
) -> PublishedLink:
    """Record a successful publish. Called right after the hosted service accepts a
    bundle — the local record is what makes revocation and "what have I published"
    possible later; nothing else in this repo tracks it."""
    link = PublishedLink(
        id=secrets.token_hex(8),
        thread_id=thread_id,
        title=title,
        public_url=public_url,
        revoke_token=revoke_token,
        published_at=time.time(),
    )
    if expires_at:
        link.expires_at = expires_at
    links = _load()
    links.insert(0, link)  # newest first, matching how the console will list them
    _save(links)
    logger.info("[publish] recorded %s (thread %s)", link.id, thread_id)
    return link


def list_published_links() -> list[dict]:
    return [link.public() for link in _load()]


def get_link(link_id: str) -> PublishedLink | None:
    """Server-internal only — the one place the raw revoke_token is read back out, to
    present it to the hosted service's revoke endpoint."""
    for link in _load():
        if link.id == link_id:
            return link
    return None


def mark_revoked(link_id: str) -> bool:
    """Flip a link to revoked LOCALLY. Callers must have already confirmed the hosted
    service accepted the revocation — marking this before that would tell the operator a
    link is dead when it's still live."""
    links = _load()
    found = False
    for link in links:
        if link.id == link_id:
            link.revoked_at = time.time()
            found = True
            break
    if found:
        _save(links)
    return found
