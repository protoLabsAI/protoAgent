"""OAuth-subscription credential resolution for the native providers (ADR 0097).

Two providers authenticate protoAgent's native pipeline with a coding-agent OAuth
subscription instead of a gateway API key. Their credential stories differ, mirroring
what Hermes does (the reference implementation):

- ``anthropic-oauth`` — READ Claude Code's own credentials live
  (``$CLAUDE_CODE_OAUTH_TOKEN`` env, or ``~/.claude/.credentials.json`` / keychain).
  Claude Code owns login *and* refresh; we borrow the live access token. Anthropic's
  Agent SDK (2026-06) explicitly licenses a third-party app authenticating with a
  user's Claude subscription, so this is sanctioned.

- ``openai-codex`` — BOOTSTRAP from the Codex CLI's store (``~/.codex/auth.json``),
  then keep and refresh our OWN copy under the instance root, so day-to-day refreshes
  never write the CLI's file. Note what owning a copy does NOT buy: the bootstrap
  *copies* the CLI's refresh token, and OpenAI's refresh tokens are single-use, so the
  first refresh after an import still rotates that token out from under the CLI — and
  out from under any sibling instance that imported the same one. That is recoverable
  rather than terminal: a rejected refresh re-imports whatever credential the CLI holds
  now (see :func:`resolve_codex_oauth`). Using ChatGPT/Codex OAuth from a third-party
  app is a grayer ToS area than the Claude path; see ADR 0097.

This module resolves *credentials only* — the ``BaseChatModel`` builders live in
:mod:`graph.providers.anthropic_oauth` and :mod:`graph.providers.openai_codex`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from infra.paths import InstancePaths, atomic_write, box_root, harden_private_file, instance_paths

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger("protoagent.providers.oauth")


# ── Credential lifecycle: per-store lock + explicit-disconnect marker ─────────
#
# ``create_llm`` resolves credentials per turn AND for aux/subagent slots, so two
# consumers in the SAME process can race the read→refresh→write on a single-use refresh
# token (#2441). A per-store ``threading.Lock`` serializes the slow (refresh/bootstrap)
# path; warm reads stay lock-free. Disconnect (#2440) takes the same lock so it can't race
# a refresh. Locks are keyed by the resolved store path so dev/prod instances are independent.
_STORE_LOCKS: dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()

# Explicit disconnect (#2440): a provider listed here must NOT auto-resolve (no Codex-CLI
# re-bootstrap, no stored/CLI Claude token) until an in-console sign-in reconnects it. The
# marker is a tiny owner-only file next to the credential stores.
_DISCONNECT_MARKER = "oauth-disconnected.json"


def _store_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = _STORE_LOCKS[key] = threading.Lock()
        return lock


@contextmanager
def _file_lock(path: Path) -> "Iterator[None]":
    """Best-effort EXCLUSIVE cross-process lock on ``path``'s sidecar, or a no-op.

    ``_store_lock`` is a ``threading.Lock`` — it serializes consumers inside ONE
    process. Once a credential store is shared by every instance on the machine, the
    racers are separate SERVER PROCESSES (the desktop app alone runs several), and a
    single-use refresh token spent twice is a 401 for whoever loses. This closes that
    window.

    Deliberately best-effort: correctness does not depend on it. A refusal that slips
    through is still recovered by re-reading the store and adopting whatever the winner
    wrote (see ``resolve_codex_oauth``), so an unlockable filesystem — a network mount,
    an exotic platform — degrades to the pre-existing behaviour rather than failing the
    turn."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    fh = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 — released in the finally below
        harden_private_file(lock_path)
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:  # noqa: BLE001 — a lock we cannot take must not break auth
        log.debug("[oauth] cross-process lock unavailable for %s", lock_path, exc_info=True)
    try:
        yield
    finally:
        if fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            fh.close()


def _disconnect_marker_path(paths: InstancePaths) -> Path:
    return paths.config_dir / _DISCONNECT_MARKER


def _disconnected_providers(paths: InstancePaths) -> set[str]:
    try:
        doc = json.loads(_disconnect_marker_path(paths).read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    return set(doc) if isinstance(doc, list) else set()


def is_disconnected(provider: str, paths: InstancePaths | None = None) -> bool:
    return provider in _disconnected_providers(paths or instance_paths())


def _write_disconnected(paths: InstancePaths, providers: set[str]) -> None:
    path = _disconnect_marker_path(paths)
    if not providers:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(sorted(providers)), mode=0o600)


def _mark_disconnected(paths: InstancePaths, provider: str) -> None:
    _write_disconnected(paths, _disconnected_providers(paths) | {provider})


def clear_disconnected(provider: str, paths: InstancePaths | None = None) -> None:
    """An explicit in-console sign-in clears the disconnect intent for ``provider``."""
    paths = paths or instance_paths()
    _write_disconnected(paths, _disconnected_providers(paths) - {provider})


class OAuthCredentialError(RuntimeError):
    """No usable OAuth credential could be resolved for a native provider.

    Carries a ``provider`` and a ``relogin`` hint so the caller (create_llm /
    a startup check) can surface an actionable message instead of a bare 401.
    """

    def __init__(self, message: str, *, provider: str, relogin: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.relogin = relogin


# ── Anthropic (Claude Code) — read-live, no store ─────────────────────────────

# The env var Claude Code sets for embedding hosts, and the two credential files
# it writes. ``CLAUDE_CODE_OAUTH_TOKEN`` also holds a `sk-ant-oat…` *setup token*
# a user can paste for a headless box.
_CLAUDE_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_CLAUDE_CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
# Refresh 60s early so a token that expires mid-turn isn't handed out.
_ANTHROPIC_REFRESH_SKEW_S = 60
# Claude Code's OAuth endpoints + public client id — used to REFRESH tokens that
# protoAgent's own in-console sign-in minted (graph/providers/oauth_login.py). See the
# ToS note there: minting via this client is opt-in and the operator's call.
_ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 — public OAuth endpoint
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


@dataclass(frozen=True)
class AnthropicOAuthCreds:
    access_token: str
    source: str  # "env" | "credentials_file" | "keychain" | "instance_store"
    expires_at: float | None = None  # epoch seconds, when known


def _read_claude_credentials_file() -> dict[str, Any] | None:
    try:
        raw = _CLAUDE_CREDS_FILE.read_text()
    except (OSError, ValueError):
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("~/.claude/.credentials.json is not valid JSON")
        return None
    return doc if isinstance(doc, dict) else None


def _read_claude_keychain() -> dict[str, Any] | None:
    """Claude Code's macOS credential store. On macOS the CLI writes the login to
    the Keychain (a "Claude Code-credentials" generic password), NOT
    ``~/.claude/.credentials.json`` — so the file-only read made the borrow-the-CLI-login
    story silently dead on the primary desktop platform (a live provider switch to
    anthropic-oauth failed "No Claude OAuth credential found" with Claude Code signed
    in right there). Same JSON document shape as the credentials file. Returns None
    off-macOS, when the item is absent, or on any error — never raises, bounded wait."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:  # absent item exits 44; locked keychain errors similarly
        return None
    try:
        doc = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _creds_from_claude_doc(doc: dict[str, Any], source: str) -> AnthropicOAuthCreds | None:
    """The ``claudeAiOauth`` document (credentials file / Keychain item) → creds.
    None when the shape/token is missing or the borrowed token is expired. Claude Code
    owns refresh for its login; handing its known-dead token to every caller only turns
    a local, actionable state into a remote 401."""
    oauth = doc.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = str(oauth.get("accessToken", "") or "").strip()
    if not token:
        return None
    exp_ms = oauth.get("expiresAt")
    expires_at = float(exp_ms) / 1000.0 if isinstance(exp_ms, (int, float)) else None
    if expires_at is not None and expires_at <= time.time() + _ANTHROPIC_REFRESH_SKEW_S:
        log.info(
            "[anthropic-oauth] ignoring expired borrowed Claude Code token from %s",
            source,
        )
        return None
    return AnthropicOAuthCreds(access_token=token, source=source, expires_at=expires_at)


def _anthropic_box_store() -> Path:
    return box_root() / "anthropic-oauth.json"


def _anthropic_store_path(paths: InstancePaths | None = None) -> Path:
    """protoAgent's own Claude token copy: an instance override if one exists, else box.

    Same scope rule as the Codex store above, for the same reason — one console sign-in
    should serve every instance on the machine, not just the one you happened to open."""
    instance = (paths or instance_paths()).config_dir / "anthropic-oauth.json"
    return instance if instance.exists() else _anthropic_box_store()


def _read_anthropic_store() -> dict[str, Any] | None:
    try:
        doc = json.loads(_anthropic_store_path().read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("access_token") else None


def _write_anthropic_store(tokens: dict[str, Any]) -> None:
    """Persist a freshly minted/refreshed Claude token set. Stamps ``expires_at`` from
    the response's ``expires_in`` so the resolver can refresh proactively."""
    access = str(tokens.get("access_token", "") or "").strip()
    if not access:
        return
    expires_in = tokens.get("expires_in")
    expires_at = _now() + float(expires_in) if isinstance(expires_in, (int, float)) else None
    doc = {
        "access_token": access,
        "refresh_token": str(tokens.get("refresh_token", "") or ""),
        "expires_at": expires_at,
    }
    path = _anthropic_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(doc), mode=0o600)


def _now() -> float:
    return time.time()


def _refresh_anthropic_tokens(refresh_token: str, *, timeout_s: float = 20.0) -> dict[str, Any]:
    resp = httpx.post(
        _ANTHROPIC_TOKEN_URL,
        json={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": _ANTHROPIC_CLIENT_ID},
        headers={"Content-Type": "application/json"},
        timeout=httpx.Timeout(max(5.0, float(timeout_s))),
    )
    if resp.status_code != 200:
        raise OAuthCredentialError(
            f"Claude token refresh failed (HTTP {resp.status_code}). Sign in again.",
            provider="anthropic-oauth",
            relogin=resp.status_code in {400, 401, 403},
        )
    tokens = resp.json()
    if not str(tokens.get("access_token", "") or ""):
        raise OAuthCredentialError(
            "Claude token refresh returned no access_token.", provider="anthropic-oauth"
        )
    return tokens


def resolve_anthropic_oauth() -> AnthropicOAuthCreds:
    """Return a live Claude OAuth access token, or raise OAuthCredentialError.

    Order (explicit intent first): ``$CLAUDE_CODE_OAUTH_TOKEN`` env → protoAgent's own
    store from in-console sign-in (refreshed when expiring) → ``~/.claude/.credentials.json``
    → the macOS Keychain item Claude Code writes on darwin (same document shape).
    Only our own store is refreshed here; Claude Code owns refresh for its file, and a
    truly-dead CLI token surfaces as a clean 401 the caller maps to a relogin hint.
    """
    env_token = os.environ.get(_CLAUDE_ENV_VAR, "").strip()
    if env_token:
        return AnthropicOAuthCreds(access_token=env_token, source="env")

    # Explicit disconnect (#2440): once disconnected in the console, don't auto-resolve from
    # our store or Claude Code's credentials until an in-console sign-in reconnects. (An
    # explicit CLAUDE_CODE_OAUTH_TOKEN env above still wins — it's deliberate config.)
    if is_disconnected("anthropic-oauth"):
        raise OAuthCredentialError(
            "Claude is disconnected in protoAgent. Sign in again to reconnect.",
            provider="anthropic-oauth",
        )

    store = _read_anthropic_store()
    if store:
        access = str(store["access_token"]).strip()
        expires_at = store.get("expires_at")
        expiring = isinstance(expires_at, (int, float)) and expires_at <= _now() + _ANTHROPIC_REFRESH_SKEW_S
        if expiring:
            refresh_token = str(store.get("refresh_token", "") or "").strip()
            if not refresh_token:
                raise OAuthCredentialError(
                    "protoAgent's stored Claude OAuth token has expired and cannot be "
                    "refreshed. Sign in again.",
                    provider="anthropic-oauth",
                )
            refreshed = _refresh_anthropic_tokens(refresh_token)
            # A refresh may not return a new refresh_token — keep the old one.
            refreshed.setdefault("refresh_token", refresh_token)
            _write_anthropic_store(refreshed)
            return AnthropicOAuthCreds(access_token=str(refreshed["access_token"]).strip(), source="instance_store")
        return AnthropicOAuthCreds(
            access_token=access,
            source="instance_store",
            expires_at=expires_at if isinstance(expires_at, (int, float)) else None,
        )

    borrowed_expired = False
    for source, doc in (
        ("credentials_file", _read_claude_credentials_file()),
        # macOS: Claude Code's login lives in the Keychain, not the credentials file.
        ("keychain", _read_claude_keychain()),
    ):
        if doc:
            oauth = doc.get("claudeAiOauth")
            exp_ms = oauth.get("expiresAt") if isinstance(oauth, dict) else None
            token = str(oauth.get("accessToken", "") or "").strip() if isinstance(oauth, dict) else ""
            borrowed_expired = borrowed_expired or (
                bool(token) and isinstance(exp_ms, (int, float))
                and float(exp_ms) / 1000.0 <= time.time() + _ANTHROPIC_REFRESH_SKEW_S
            )
            creds = _creds_from_claude_doc(doc, source)
        else:
            creds = None
        if creds:
            return creds

    if borrowed_expired:
        raise OAuthCredentialError(
            "Claude Code's borrowed OAuth token has expired. Sign in from protoAgent to "
            "create an independently refreshable login, or re-authenticate Claude Code.",
            provider="anthropic-oauth",
        )

    raise OAuthCredentialError(
        "No Claude OAuth credential found. Sign in from the console, the Claude Code CLI "
        "(`claude`), or set CLAUDE_CODE_OAUTH_TOKEN to a setup token (`claude setup-token`).",
        provider="anthropic-oauth",
    )


# ── OpenAI Codex — bootstrap-then-own, with refresh ───────────────────────────

_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"  # noqa: S105 — public OAuth endpoint
_CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_CLI_AUTH_FILE = Path.home() / ".codex" / "auth.json"
_CODEX_REFRESH_SKEW_S = 120
_CODEX_USER_AGENT = "protoAgent-codex/0.1 (+https://github.com/protoLabsAI/protoAgent)"


@dataclass(frozen=True)
class CodexOAuthCreds:
    access_token: str
    account_id: str | None
    base_url: str
    source: str  # "instance_store" | "codex_cli_bootstrap"


# A ChatGPT or Claude login belongs to the PERSON at this machine, not to one agent
# instance. Keeping the copy per-instance meant a console sign-in on `default` did
# nothing for the desktop app's other servers, the dev sandbox, or any other instance:
# each one fell back to importing the vendor CLI's single-use token and burning it for
# the rest (#3112). So the store lives at the BOX tier — beside `host-config.yaml` and
# the other things every instance on this machine already shares — and an instance-local
# file, when one exists, overrides it.
#
# Mirrors Hermes's credential pool (profile entries win, the global root is the fallback
# "so workers spawned in a profile can see providers that were only authenticated at
# global scope"), with the default flipped to box: Hermes profiles are deliberate
# isolation, whereas protoAgent instances are mostly incidental.


def _codex_box_store() -> Path:
    return box_root() / "codex-oauth.json"


def _codex_store_path(paths: InstancePaths) -> Path:
    """The Codex store to read AND write: an instance override if one exists, else box.

    Existence is the whole switch, which is what keeps this a no-op upgrade — every
    instance that already has its own file keeps using it, and only instances that never
    had one start sharing. Writing back to whichever we read means a per-instance account
    stays per-instance rather than silently migrating to the shared one on first refresh.
    """
    instance = paths.config_dir / "codex-oauth.json"
    return instance if instance.exists() else _codex_box_store()


class OAuthStoreTransferError(RuntimeError):
    """A legacy instance OAuth store could not safely join the shared box tier."""


def promote_instance_oauth_to_box(config_dir: Path) -> list[str]:
    """Move legacy instance-local OAuth stores into the shared box tier.

    Fleet sisters must resolve one credential store, not receive independent copies:
    both OAuth refresh tokens are mutable and Codex refresh tokens are single-use.  A
    copy would let two member processes rotate the same token independently and strand
    whichever loses the race.  New sign-ins already write to the box tier (#3112); this
    helper is only the upgrade bridge for an older instance override encountered while
    creating a sister.

    A distinct existing box credential is a refusal: silently replacing a machine-wide
    login or giving the new sister a different account would both violate the operator's
    intent.  Byte-identical overrides are deduplicated so the source instance rejoins the
    shared store.  Returns the filenames promoted or deduplicated.
    """
    candidates = [
        (Path(config_dir) / filename, destination, filename, provider)
        for filename, destination, provider in (
            ("codex-oauth.json", _codex_box_store(), "openai-codex"),
            ("anthropic-oauth.json", _anthropic_box_store(), "anthropic-oauth"),
        )
        if (Path(config_dir) / filename).exists() and (Path(config_dir) / filename) != destination
    ]
    if not candidates:
        return []

    promoted: list[str] = []
    try:
        # Lock every candidate before the conflict preflight, then mutate.  No other
        # runtime path takes two stores, so this stable order cannot deadlock with a
        # provider refresh/disconnect and prevents a TOCTOU overwrite of either side.
        with ExitStack() as locks:
            for source, destination, _filename, _provider in candidates:
                locks.enter_context(_store_lock(source))
                locks.enter_context(_file_lock(source))
                locks.enter_context(_store_lock(destination))
                locks.enter_context(_file_lock(destination))

            payloads: list[tuple[Path, Path, str, str, bool]] = []
            conflicts: list[str] = []
            try:
                marker_doc = json.loads((Path(config_dir) / _DISCONNECT_MARKER).read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                marker_doc = []
            disconnected = set(marker_doc) if isinstance(marker_doc, list) else set()
            blocked = [provider for _source, _destination, _filename, provider in candidates if provider in disconnected]
            if blocked:
                raise OAuthStoreTransferError(
                    "cannot share legacy instance OAuth for "
                    + ", ".join(blocked)
                    + ": this instance explicitly disconnected it. Reconnect first, then create the sister again."
                )

            for source, destination, filename, _provider in candidates:
                if not source.exists():
                    continue
                payload = source.read_text(encoding="utf-8")
                if destination.exists() and destination.read_text(encoding="utf-8") != payload:
                    conflicts.append(filename)
                payloads.append((source, destination, filename, payload, destination.exists()))
            if conflicts:
                names = ", ".join(conflicts)
                raise OAuthStoreTransferError(
                    f"cannot share legacy instance OAuth ({names}): the box already has a different login. "
                    "Reconnect this instance after removing its local OAuth override, then create the sister again."
                )

            # Phase 1 copies every absent destination while every source remains the
            # authoritative store.  If any write fails, remove only destinations this
            # transaction created; no source ownership has changed.
            created: list[Path] = []
            try:
                for _source, destination, _filename, payload, existed in payloads:
                    if not existed:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write(destination, payload, mode=0o600)
                        created.append(destination)
            except OSError:
                for destination in created:
                    destination.unlink(missing_ok=True)
                raise

            # Phase 2 commits ownership by removing the overrides only after every box
            # store is durable.  Roll back removed sources + newly-created destinations
            # if a later removal fails, so a two-provider transfer cannot half-complete.
            removed: list[tuple[Path, str]] = []
            try:
                for source, _destination, filename, payload, _existed in payloads:
                    source.unlink()
                    removed.append((source, payload))
                    promoted.append(filename)
            except OSError:
                for source, payload in removed:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(source, payload, mode=0o600)
                for destination in created:
                    destination.unlink(missing_ok=True)
                raise
    except OAuthStoreTransferError:
        raise
    except OSError as exc:
        raise OAuthStoreTransferError(
            "could not transfer the legacy instance OAuth login into the shared box store"
        ) from exc
    return promoted


def _b64url_json(segment: str) -> dict[str, Any]:
    """Decode one base64url JWT segment to a JSON object (no signature check —
    we only read the account-id claim, never trust it for auth)."""
    pad = "=" * (-len(segment) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(segment + pad))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return {}


def _codex_account_id(tokens: dict[str, Any]) -> str | None:
    """The ChatGPT account id for the ``ChatGPT-Account-Id`` header.

    Prefer an explicit ``account_id`` field; otherwise pull the
    ``chatgpt_account_id`` claim out of the id_token (or access_token) JWT.
    """
    explicit = tokens.get("account_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for key in ("id_token", "access_token"):
        jwt = tokens.get(key)
        if not isinstance(jwt, str) or jwt.count(".") != 2:
            continue
        claims = _b64url_json(jwt.split(".")[1])
        auth = claims.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            acct = auth.get("chatgpt_account_id") or auth.get("chatgpt_user_id")
            if isinstance(acct, str) and acct.strip():
                return acct.strip()
    return None


def _jwt_is_expiring(access_token: str, skew_s: int) -> bool:
    """True if the access-token JWT's ``exp`` is within ``skew_s`` (or unreadable)."""
    if not isinstance(access_token, str) or access_token.count(".") != 2:
        return True
    exp = _b64url_json(access_token.split(".")[1]).get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return float(exp) <= time.time() + skew_s


def _codex_needs_refresh(tokens: dict[str, Any], skew_s: int) -> bool:
    """Should we spend the refresh token now?

    Two signals, and the provider's wins while the access token is still usable:
    OpenAI's ``earliest_refresh_at`` says the soonest it wants to hear from us, so a
    token that is live and before that mark is simply used. Once the access token is
    genuinely within ``skew_s`` of expiry we refresh regardless — a stale
    ``earliest_refresh_at`` must never strand a caller.
    """
    access = str(tokens.get("access_token", "") or "").strip()
    if not access or _jwt_is_expiring(access, skew_s):
        return True
    earliest = tokens.get("earliest_refresh_at")
    if isinstance(earliest, (int, float)):
        return time.time() >= float(earliest)
    return False


def _read_codex_tokens(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    tokens = doc.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def _refresh_codex_tokens(tokens: dict[str, Any], *, timeout_s: float = 20.0) -> dict[str, Any]:
    """POST the refresh grant to OpenAI's token endpoint; return updated tokens.

    Mirrors Hermes ``refresh_codex_oauth_pure`` — same client_id and endpoint.
    Preserves ``refresh_token`` when the response rotates it (falls back to the old
    one) and the ``account_id`` we already resolved.
    """
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise OAuthCredentialError(
            "Codex credentials are missing a refresh_token. Re-run `codex` to sign in.",
            provider="openai-codex",
        )
    try:
        resp = httpx.post(
            _CODEX_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _CODEX_USER_AGENT,
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _CODEX_CLIENT_ID,
            },
            timeout=httpx.Timeout(max(5.0, float(timeout_s))),
        )
    except httpx.HTTPError as exc:
        raise OAuthCredentialError(
            f"Codex token refresh could not reach OpenAI: {exc}",
            provider="openai-codex",
            relogin=False,
        ) from exc

    if resp.status_code != 200:
        relogin = resp.status_code in {400, 401, 403}
        raise OAuthCredentialError(
            f"Codex token refresh failed (HTTP {resp.status_code}) — OpenAI rejected the "
            "refresh token, usually because it had already been spent. Run `codex login` "
            "to mint a fresh credential; protoAgent re-imports it on the next attempt.",
            provider="openai-codex",
            relogin=relogin,
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OAuthCredentialError(
            "Codex token refresh returned invalid JSON.", provider="openai-codex"
        ) from exc

    new_access = str(payload.get("access_token", "") or "").strip()
    if not new_access:
        raise OAuthCredentialError(
            "Codex token refresh response was missing access_token.",
            provider="openai-codex",
        )
    updated = dict(tokens)
    updated["access_token"] = new_access
    # OpenAI returns `earliest_refresh_at` — an explicit "do not refresh before this",
    # about a day ahead of a 10-day access token. Honouring it turns refresh from a
    # per-boot expiry check into a roughly once-per-nine-days event, which is the real
    # reason concurrent refreshes were ever common enough to notice. Kept alongside the
    # tokens so every process on the box sees the same answer.
    if isinstance(payload.get("earliest_refresh_at"), (int, float)):
        updated["earliest_refresh_at"] = float(payload["earliest_refresh_at"])
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"].strip():
        updated["refresh_token"] = payload["refresh_token"].strip()
    if isinstance(payload.get("id_token"), str) and payload["id_token"].strip():
        updated["id_token"] = payload["id_token"].strip()
    return updated


# Credential provenance (#2461): who minted the token set this store holds.
# "cli_bootstrap" — copied from the Codex CLI's auth.json; the login is SHARED
# with another application, so protoAgent must never remotely revoke it.
# "device_login" — minted by protoAgent's own in-console device sign-in; ours to
# revoke. Stores written before this field exist ("" on read) and are treated as
# borrowed: with ownership unproven, deleting our copy is the only safe scope.
PROVENANCE_CLI_BOOTSTRAP = "cli_bootstrap"
PROVENANCE_DEVICE_LOGIN = "device_login"


def _read_codex_provenance(path: Path) -> str:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    return str(doc.get("provenance", "") or "") if isinstance(doc, dict) else ""


def _write_codex_store(path: Path, tokens: dict[str, Any], provenance: str | None = None) -> None:
    """Persist the token set. ``provenance=None`` (the refresh path) preserves
    whatever the store already recorded — a refresh rotates tokens, it does not
    change who minted the login."""
    if provenance is None:
        provenance = _read_codex_provenance(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"tokens": tokens, "last_refresh": time.time()}
    if provenance:
        doc["provenance"] = provenance
    atomic_write(path, json.dumps(doc), mode=0o600)


def _usable_cli_tokens() -> dict[str, Any] | None:
    """The Codex CLI's tokens, but ONLY when they can actually still be used.

    Importing is a one-way copy of a SINGLE-USE refresh token. If the CLI's own access
    token has already expired, its refresh token is almost certainly the one that was
    already spent — by us, by a sibling instance, or by the CLI itself — so importing the
    pair buys a 401 on the very next call and an error that blames a refresh instead of
    naming the missing login. Hermes rejects a stale pair for exactly this reason:
    "importing stale tokens from ~/.codex/ that can't be refreshed leaves the user stuck
    with 'Login successful!' but no working credentials."

    Both halves are required: adopting an access token with no refresh token would only
    move the failure to the next refresh cycle.
    """
    tokens = _read_codex_tokens(_CODEX_CLI_AUTH_FILE)
    if not tokens:
        return None
    access = str(tokens.get("access_token", "") or "").strip()
    refresh = str(tokens.get("refresh_token", "") or "").strip()
    if not access or not refresh:
        return None
    # Skew 0 deliberately: this asks "is it dead yet", not "should we refresh soon" —
    # a live-but-expiring CLI token is still worth adopting, we just refresh it after.
    if _jwt_is_expiring(access, 0):
        log.info(
            "[codex] %s holds an expired login; not importing it — a stale refresh token "
            "would 401 rather than sign in.",
            _CODEX_CLI_AUTH_FILE,
        )
        return None
    return tokens


def _peer_rotated_tokens(store: Path, spent: dict[str, Any]) -> dict[str, Any] | None:
    """The store's CURRENT tokens when a peer process rotated them under us, else None.

    With a box-level store the racers are separate processes, so the losing one's 401 is
    routinely not "the login is dead" but "someone else just refreshed". Re-reading costs
    a file read and turns that into a success."""
    current = _read_codex_tokens(store)
    if not current:
        return None
    if not str(current.get("access_token", "") or "").strip():
        return None
    ours = str(spent.get("refresh_token", "") or "").strip()
    theirs = str(current.get("refresh_token", "") or "").strip()
    return current if theirs and theirs != ours else None


def _cli_recovery_tokens(
    exc: OAuthCredentialError,
    source: str,
    tokens: dict[str, Any],
    paths: InstancePaths,
) -> dict[str, Any] | None:
    """The Codex CLI's tokens when they can rescue a *rejected* refresh, else ``None``.

    Our stored refresh token is single-use and shared by construction: every instance
    that bootstraps from the same ``~/.codex/auth.json`` copies the same one, so the
    first to refresh burns it for the rest. Recovery is deliberately narrow — it applies
    only when

    * OpenAI *rejected* the token (``exc.relogin``); a network blip must not re-import,
    * we were spending our OWN store (a fresh bootstrap that 401s is already terminal),
    * the provider is not explicitly disconnected (#2440) — that intent outranks repair,
    * and the CLI holds a genuinely DIFFERENT refresh token. Re-spending the identical
      dead token would just 401 again, and could burn a token someone else still holds.
    """
    if not exc.relogin or source != "instance_store":
        return None
    if is_disconnected("openai-codex", paths):
        return None
    # A *live* CLI login only. "Different from ours" is not enough: after any successful
    # refresh our token is NEWER than the CLI's, so a bare difference check would happily
    # adopt the older, already-spent pair and 401 again.
    cli = _usable_cli_tokens()
    if cli is None:
        return None
    theirs = str(cli.get("refresh_token", "") or "").strip()
    ours = str(tokens.get("refresh_token", "") or "").strip()
    return cli if theirs and theirs != ours else None


def note_codex_auth_rejected(paths: InstancePaths | None = None) -> bool:
    """Record that the stored ACCESS token was rejected, so the next resolve refreshes.

    An access token can stop working long before its ``exp``: signing in again on the
    same ChatGPT account invalidates the previous session, and the backend then answers
    ``401 token_invalidated`` while the JWT still claims to be valid for days. Resolution
    decides "is this good?" from ``exp`` alone, so nothing ever refreshed and every call
    failed identically until a human intervened — even though the refresh token was live
    the whole time and one refresh would have fixed it.

    Blanking the access token (keeping the refresh token) is the smallest durable way to
    say "don't trust this one again": the next resolve takes the refresh path by its
    existing rules, no new state and no network call here. Returns whether anything
    changed, so a caller can tell a real invalidation from a repeat.
    """
    paths = paths or instance_paths()
    store = _codex_store_path(paths)
    with _store_lock(store), _file_lock(store):
        tokens = _read_codex_tokens(store)
        if not tokens or not str(tokens.get("access_token", "") or "").strip():
            return False
        cleared = dict(tokens)
        cleared["access_token"] = ""
        # `earliest_refresh_at` is the provider's "don't refresh before this" hint; an
        # invalidated token must not be pinned behind it, so it goes too.
        cleared.pop("earliest_refresh_at", None)
        _write_codex_store(store, cleared)
    log.warning("[codex] the stored access token was rejected; the next call will refresh it.")
    return True


def resolve_codex_oauth(paths: InstancePaths | None = None, *, force_refresh: bool = False) -> CodexOAuthCreds:
    """Return a fresh Codex access token + account id, refreshing/bootstrapping as needed.

    1. Read our own instance-scoped store; if absent, bootstrap it once from the
       Codex CLI's ``~/.codex/auth.json``.
    2. If the access token is expiring, refresh against OpenAI and persist our copy
       (we never write the CLI's file, though the refresh does rotate the token it holds).
    3. If that refresh is REJECTED, our stored token had already been spent — by a sibling
       instance that bootstrapped from the same CLI login, or by the CLI itself. Re-import
       the CLI's current credential when it differs from ours, so a fresh ``codex login``
       is enough to recover. Without this the 401 is permanent: step 1 only bootstraps
       when the store file is *missing*, so a store holding a dead token never re-reads
       the CLI file and the error's own advice cannot work.

    Serialized per store (#2441): concurrent resolutions can't both spend the same
    single-use refresh token — a warm read is lock-free, but the refresh/bootstrap path
    takes the store lock and re-reads, so a waiter reuses the token the first caller minted.
    """
    paths = paths or instance_paths()
    store = _codex_store_path(paths)

    def _creds(tokens: dict[str, Any], access: str, source: str) -> CodexOAuthCreds:
        base_url = os.environ.get("PROTOAGENT_CODEX_BASE_URL", "").strip().rstrip("/") or _CODEX_DEFAULT_BASE_URL
        return CodexOAuthCreds(access_token=access, account_id=_codex_account_id(tokens), base_url=base_url, source=source)

    # Fast path: a warm, unexpired store read needs neither the lock nor a write.
    tokens = _read_codex_tokens(store)
    if tokens and not force_refresh and not _codex_needs_refresh(tokens, _CODEX_REFRESH_SKEW_S):
        return _creds(tokens, str(tokens["access_token"]).strip(), "instance_store")

    # Slow path: refresh or first bootstrap — serialized so single-use refresh is spent once.
    with _store_lock(store), _file_lock(store):
        tokens = _read_codex_tokens(store)  # re-read: a peer may have refreshed while we waited
        source = "instance_store"
        if tokens is None:
            if is_disconnected("openai-codex", paths):
                raise OAuthCredentialError(
                    "Codex is disconnected in protoAgent. Sign in again to reconnect.",
                    provider="openai-codex",
                )
            # NO silent import. A refresh token is single-use, so reading the Codex
            # CLI's file makes two applications hold one secret with no way to
            # coordinate — we cannot lock the CLI, and whichever refreshes first
            # silently kills the other. Every 401 this module has had to survive came
            # from that, so ownership is now explicit: protoAgent uses a credential it
            # minted (console device sign-in) or one the operator deliberately handed
            # over (`import_codex_cli_credential`, which takes ownership immediately).
            raise OAuthCredentialError(
                "Not signed in to ChatGPT. Sign in from the console — Settings ▸ Model ▸ "
                "Connected account — which mints protoAgent's own credential and never "
                "touches the Codex CLI's. If you would rather hand over the login the "
                "Codex CLI already holds, import it explicitly; that transfers ownership, "
                "so the CLI itself will need `codex login` afterwards.",
                provider="openai-codex",
            )

        access = str(tokens.get("access_token", "") or "").strip()
        refreshed = False
        if force_refresh or _codex_needs_refresh(tokens, _CODEX_REFRESH_SKEW_S):
            try:
                tokens = _refresh_codex_tokens(tokens)
            except OAuthCredentialError as exc:
                # A peer PROCESS sharing the box store may have rotated the token
                # between our read and our spend — the file on disk is then already the
                # answer. Cheaper and more likely than the CLI path below, so try it
                # first; unlike the CLI it needs no freshness test, because whatever a
                # peer just wrote is by construction newer than what we spent.
                peer = _peer_rotated_tokens(store, tokens) if exc.relogin else None
                if peer is not None:
                    log.info("[codex] refresh token was rotated by a peer process; adopting its result.")
                    tokens = peer
                    access = str(tokens["access_token"]).strip()
                    if not access or _jwt_is_expiring(access, _CODEX_REFRESH_SKEW_S):
                        tokens = _refresh_codex_tokens(tokens)
                        access = str(tokens["access_token"]).strip()
                    return _creds(tokens, access, "instance_store")
                recovered = _cli_recovery_tokens(exc, source, tokens, paths)
                if recovered is None:
                    raise
                log.warning(
                    "Codex refresh token was rejected (already spent by the CLI or a "
                    "sibling instance); re-importing the newer credential from %s.",
                    _CODEX_CLI_AUTH_FILE,
                )
                tokens, source = recovered, "codex_cli_bootstrap"
                cli_access = str(tokens.get("access_token", "") or "").strip()
                if not cli_access or _jwt_is_expiring(cli_access, _CODEX_REFRESH_SKEW_S):
                    tokens = _refresh_codex_tokens(tokens)
            access = str(tokens["access_token"]).strip()
            refreshed = True

        if source == "codex_cli_bootstrap":
            # Stamp the borrowed origin (#2461) — disconnect uses it to scope
            # itself to our copy instead of revoking a login the CLI still holds.
            _write_codex_store(store, tokens, provenance=PROVENANCE_CLI_BOOTSTRAP)
        elif refreshed:
            _write_codex_store(store, tokens)
        return _creds(tokens, access, "instance_store" if refreshed else source)


def import_codex_cli_credential(paths: InstancePaths | None = None) -> dict[str, Any]:
    """Take over the login the Codex CLI holds. Explicit, operator-initiated only.

    An OAuth refresh token is single-use, so two applications cannot both hold one
    live. Importing therefore *transfers* the login rather than copying it: we refresh
    immediately, which rotates the token to one only protoAgent knows, and the Codex
    CLI's own copy is dead from that moment. That consequence is real and is why this
    is never automatic — silently breaking another application the operator relies on
    is not ours to do. The caller is expected to have said so in the UI.

    Returns ``{"account_id": …, "cli_needs_relogin": True}``.
    """
    paths = paths or instance_paths()
    store = _codex_store_path(paths)
    tokens = _usable_cli_tokens()
    if tokens is None:
        raise OAuthCredentialError(
            "The Codex CLI has no usable login to import — its stored token is missing or "
            "already expired. Sign in from the console instead (that mints protoAgent's own "
            "credential), or run `codex login` first if you specifically want to hand that "
            "login over. Note `codex login status` reports a long-dead login as healthy.",
            provider="openai-codex",
        )
    with _store_lock(store), _file_lock(store):
        # Refresh AS PART OF the import, not later: until we have rotated, both
        # applications believe they hold a live token and the first to refresh kills the
        # other. Rotating here makes the handover atomic from the operator's point of view.
        tokens = _refresh_codex_tokens(tokens)
        _write_codex_store(store, tokens, provenance=PROVENANCE_CLI_BOOTSTRAP)
    clear_disconnected("openai-codex", paths)
    log.info("[codex] imported the Codex CLI login and rotated it; the CLI now needs `codex login`.")
    return {"account_id": _codex_account_id(tokens) or "", "cli_needs_relogin": True}


# ── Disconnect / revoke lifecycle (#2440) ─────────────────────────────────────

_CODEX_REVOKE_URL = "https://auth.openai.com/oauth/revoke"  # noqa: S105 — public OAuth endpoint


@dataclass(frozen=True)
class DisconnectResult:
    provider: str
    removed: bool  # protoAgent's own credential store was deleted
    revoked: bool  # remote revocation succeeded (best-effort; OpenAI only)
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "removed": self.removed, "revoked": self.revoked, "note": self.note}


def _revoke_codex_token(tokens: dict[str, Any], *, timeout_s: float = 8.0) -> bool:
    """Best-effort revoke of protoAgent's OpenAI token (refresh first, then access). Never
    raises — a failed/unreachable revoke must not block local deletion."""
    for hint in ("refresh_token", "access_token"):
        tok = str(tokens.get(hint, "") or "").strip()
        if not tok:
            continue
        try:
            resp = httpx.post(
                _CODEX_REVOKE_URL,
                data={"client_id": _CODEX_CLIENT_ID, "token": tok, "token_type_hint": hint},
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _CODEX_USER_AGENT},
                timeout=httpx.Timeout(max(3.0, float(timeout_s))),
            )
            if resp.status_code in (200, 204):
                return True
        except httpx.HTTPError:
            continue
    return False


def disconnect(provider: str, paths: InstancePaths | None = None) -> DisconnectResult:
    """Idempotent disconnect for a native OAuth provider (#2440).

    Attempts best-effort remote revocation for protoAgent-owned tokens, ALWAYS deletes
    protoAgent's own credential store (even when revocation fails), and
    marks the provider disconnected so it won't auto-resolve until an in-console sign-in
    reconnects it. The vendor CLI's own auth file (``~/.codex/auth.json`` /
    ``~/.claude/.credentials.json``) is never modified. Takes the same per-store lock as
    resolution, so it can't race a refresh that would rewrite the store after deletion.

    **Scope note:** the store deleted is whichever one resolution reads — an instance
    override when that instance has its own file, otherwise the BOX store shared by every
    instance on this machine. Disconnecting a shared login therefore disconnects it
    everywhere, which is the honest reading of "disconnect my ChatGPT account" but is not
    what a per-instance mental model expects; ``DisconnectResult.note`` says which
    happened so the console can too. The *marker* that suppresses re-import stays
    per-instance either way.
    """
    provider = (provider or "").strip().lower()
    paths = paths or instance_paths()
    if provider == "openai-codex":
        store = _codex_store_path(paths)
        with _store_lock(store):
            tokens = _read_codex_tokens(store)  # our copy only — never ~/.codex/auth.json
            # Ownership gate (#2461): a bootstrap-derived token set is the Codex
            # CLI's login, borrowed — remote revocation would sign the CLI out
            # too, well outside protoAgent's mandate. Only a credential our own
            # device sign-in minted is ours to revoke; a legacy store with no
            # provenance is treated as borrowed (ownership unproven).
            owned = _read_codex_provenance(store) == PROVENANCE_DEVICE_LOGIN
            revoked = _revoke_codex_token(tokens) if (tokens and owned) else False
            existed = store.exists()
            store.unlink(missing_ok=True)
            _mark_disconnected(paths, provider)
        if not existed:
            note = "already disconnected"
        elif revoked:
            note = "revoked at OpenAI and removed protoAgent's local copy"
        elif not owned:
            note = (
                "removed protoAgent's borrowed copy — the login is shared with the "
                "Codex CLI, so it was not revoked remotely"
            )
        else:
            note = "removed protoAgent's local copy (remote revoke did not confirm)"
        if existed and store == _codex_box_store():
            note += " (the shared login for every instance on this machine)"
        return DisconnectResult(provider, removed=existed, revoked=revoked, note=note)
    if provider == "anthropic-oauth":
        store = _anthropic_store_path(paths)
        with _store_lock(store):
            existed = store.exists()
            store.unlink(missing_ok=True)
            _mark_disconnected(paths, provider)
        # Anthropic has no token-revoke endpoint for these tokens; local removal +
        # suppression is the contract. Claude Code's own credentials are untouched.
        return DisconnectResult(
            provider, removed=existed, revoked=False,
            note="removed protoAgent's Claude token; sign in again to reconnect",
        )
    raise OAuthCredentialError(f"not a native OAuth provider: {provider!r}", provider=provider)
