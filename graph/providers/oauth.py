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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from infra.paths import InstancePaths, atomic_write, instance_paths

log = logging.getLogger("protoagent.providers.oauth")


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
    source: str  # "env" | "credentials_file" | "instance_store"
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
    store from in-console sign-in (refreshed when expiring) → ``~/.claude/.credentials.json``.
    Only our own store is refreshed here; Claude Code owns refresh for its file, and a
    truly-dead CLI token surfaces as a clean 401 the caller maps to a relogin hint.
    """
    env_token = os.environ.get(_CLAUDE_ENV_VAR, "").strip()
    if env_token:
        return AnthropicOAuthCreds(access_token=env_token, source="env")

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
        oauth = doc.get("claudeAiOauth")
        if isinstance(oauth, dict):
            token = str(oauth.get("accessToken", "") or "").strip()
            if token:
                exp_ms = oauth.get("expiresAt")
                expires_at = float(exp_ms) / 1000.0 if isinstance(exp_ms, (int, float)) else None
                if expires_at is not None and expires_at <= time.time() - _ANTHROPIC_REFRESH_SKEW_S:
                    log.info(
                        "[anthropic-oauth] Claude Code token looks expired (run any `claude` "
                        "command to refresh); trying it anyway",
                    )
                return AnthropicOAuthCreds(
                    access_token=token, source="credentials_file", expires_at=expires_at
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
        doc = json.loads(path.read_text())
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


def _write_codex_store(path: Path, tokens: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"tokens": tokens, "last_refresh": time.time()}), mode=0o600)


def resolve_codex_oauth(paths: InstancePaths | None = None) -> CodexOAuthCreds:
    """Return a fresh Codex access token + account id, refreshing/bootstrapping as needed.

    1. Read our own instance-scoped store; if absent, bootstrap it once from the
       Codex CLI's ``~/.codex/auth.json``.
    2. If the access token is expiring, refresh against OpenAI and persist our copy
       (never the Codex CLI's file — we don't rotate its single-use token).
    """
    paths = paths or instance_paths()
    store = _codex_store_path(paths)

    tokens = _read_codex_tokens(store)
    source = "instance_store"
    if tokens is None:
        tokens = _read_codex_tokens(_CODEX_CLI_AUTH_FILE)
        source = "codex_cli_bootstrap"
        if tokens is None:
            raise OAuthCredentialError(
                "No Codex OAuth credential found. Sign in with the Codex CLI "
                "(`codex`), then retry — protoAgent imports it once and keeps its "
                "own refreshed copy.",
                provider="openai-codex",
            )

    access_token = str(tokens.get("access_token", "") or "").strip()
    refreshed = False
    if not access_token or _jwt_is_expiring(access_token, _CODEX_REFRESH_SKEW_S):
        tokens = _refresh_codex_tokens(tokens)
        access_token = str(tokens["access_token"]).strip()
        refreshed = True

    # Persist only when we bootstrapped from the CLI file or minted a new token —
    # not on every turn (create_llm runs per turn + aux slots). A warm, unexpired
    # store read touches no disk.
    if source == "codex_cli_bootstrap" or refreshed:
        _write_codex_store(store, tokens)
    if refreshed:
        source = "instance_store"

    base_url = os.environ.get("PROTOAGENT_CODEX_BASE_URL", "").strip().rstrip("/") or _CODEX_DEFAULT_BASE_URL
    return CodexOAuthCreds(
        access_token=access_token,
        account_id=_codex_account_id(tokens),
        base_url=base_url,
        source=source,
    )
