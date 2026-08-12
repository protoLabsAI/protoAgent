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
  then keep and refresh our OWN copy under the instance root. OAuth refresh tokens
  are single-use, so sharing one file with the Codex CLI means the two clients rotate
  each other's tokens and race to 401 — owning our copy avoids that. Using ChatGPT/
  Codex OAuth from a third-party app is a grayer ToS area than the Claude path; see
  ADR 0097.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from infra.paths import InstancePaths, atomic_write, instance_paths

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
    None when the shape/token is missing. Expiry is advisory only — Claude Code owns
    refresh for its own login; a dead token surfaces as a clean 401 → relogin hint."""
    oauth = doc.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = str(oauth.get("accessToken", "") or "").strip()
    if not token:
        return None
    exp_ms = oauth.get("expiresAt")
    expires_at = float(exp_ms) / 1000.0 if isinstance(exp_ms, (int, float)) else None
    if expires_at is not None and expires_at <= time.time() - _ANTHROPIC_REFRESH_SKEW_S:
        log.info(
            "[anthropic-oauth] Claude Code token looks expired (run any `claude` "
            "command to refresh); trying it anyway",
        )
    return AnthropicOAuthCreds(access_token=token, source=source, expires_at=expires_at)


def _anthropic_store_path(paths: InstancePaths | None = None) -> Path:
    """protoAgent's own Claude token copy (from in-console sign-in), instance-scoped."""
    return (paths or instance_paths()).config_dir / "anthropic-oauth.json"


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
        if expiring and str(store.get("refresh_token", "") or ""):
            refreshed = _refresh_anthropic_tokens(str(store["refresh_token"]))
            # A refresh may not return a new refresh_token — keep the old one.
            refreshed.setdefault("refresh_token", store["refresh_token"])
            _write_anthropic_store(refreshed)
            return AnthropicOAuthCreds(access_token=str(refreshed["access_token"]).strip(), source="instance_store")
        return AnthropicOAuthCreds(
            access_token=access,
            source="instance_store",
            expires_at=expires_at if isinstance(expires_at, (int, float)) else None,
        )

    doc = _read_claude_credentials_file()
    if doc:
        creds = _creds_from_claude_doc(doc, "credentials_file")
        if creds:
            return creds

    # macOS: Claude Code's login lives in the Keychain, not the credentials file.
    doc = _read_claude_keychain()
    if doc:
        creds = _creds_from_claude_doc(doc, "keychain")
        if creds:
            return creds

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


def _codex_store_path(paths: InstancePaths) -> Path:
    """Our own Codex token copy, instance-scoped (dev and prod never share)."""
    return paths.config_dir / "codex-oauth.json"


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
            f"Codex token refresh failed (HTTP {resp.status_code}). "
            "Re-run `codex` in your terminal to generate fresh tokens.",
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


def resolve_codex_oauth(paths: InstancePaths | None = None) -> CodexOAuthCreds:
    """Return a fresh Codex access token + account id, refreshing/bootstrapping as needed.

    1. Read our own instance-scoped store; if absent, bootstrap it once from the
       Codex CLI's ``~/.codex/auth.json``.
    2. If the access token is expiring, refresh against OpenAI and persist our copy
       (never the Codex CLI's file — we don't rotate its single-use token).

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
    if tokens:
        access = str(tokens.get("access_token", "") or "").strip()
        if access and not _jwt_is_expiring(access, _CODEX_REFRESH_SKEW_S):
            return _creds(tokens, access, "instance_store")

    # Slow path: refresh or first bootstrap — serialized so single-use refresh is spent once.
    with _store_lock(store):
        tokens = _read_codex_tokens(store)  # re-read: a peer may have refreshed while we waited
        source = "instance_store"
        if tokens is None:
            # Respect an explicit disconnect: do NOT silently re-import the Codex CLI's
            # credential until an in-console sign-in reconnects (#2440).
            if is_disconnected("openai-codex", paths):
                raise OAuthCredentialError(
                    "Codex is disconnected in protoAgent. Sign in again to reconnect.",
                    provider="openai-codex",
                )
            tokens = _read_codex_tokens(_CODEX_CLI_AUTH_FILE)
            source = "codex_cli_bootstrap"
            if tokens is None:
                raise OAuthCredentialError(
                    "No Codex OAuth credential found. Sign in with the Codex CLI "
                    "(`codex`), then retry — protoAgent imports it once and keeps its "
                    "own refreshed copy.",
                    provider="openai-codex",
                )

        access = str(tokens.get("access_token", "") or "").strip()
        refreshed = False
        if not access or _jwt_is_expiring(access, _CODEX_REFRESH_SKEW_S):
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
    protoAgent's own instance-scoped credential store (even when revocation fails), and
    marks the provider disconnected so it won't auto-resolve until an in-console sign-in
    reconnects it. The vendor CLI's own auth file (``~/.codex/auth.json`` /
    ``~/.claude/.credentials.json``) is never modified. Takes the same per-store lock as
    resolution, so it can't race a refresh that would rewrite the store after deletion.
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
