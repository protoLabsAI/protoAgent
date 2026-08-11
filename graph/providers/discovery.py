"""Console read-layer for native OAuth providers (ADR 0097).

Powers the setup wizard + Settings: "is the user signed in?" and "what models can
this subscription run?" — so nobody has to hand-edit YAML or guess a model id. Both
answers are read-only probes; nothing here refreshes or writes a token (the status
check must be a safe, repeatable poll).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import httpx

from graph.providers import NATIVE_OAUTH_PROVIDERS
from graph.providers import oauth as _oauth

if TYPE_CHECKING:
    from graph.config import LangGraphConfig

log = logging.getLogger("protoagent.providers.discovery")


# How long a credential survives without a human (#2549). `signed_in: true` covers
# three materially different situations, and a fleet dashboard that sees only the
# boolean can't tell "durable" from "decaying".
DURABILITY_MANAGED = "managed"  # our store, our refresh token — refreshed on use
DURABILITY_BORROWED = "borrowed"  # a vendor CLI's login: alive while THAT human uses it
DURABILITY_STATIC = "static"  # an env token: never refreshed, never inspectable


@dataclass(frozen=True)
class OAuthStatus:
    provider: str
    signed_in: bool
    source: str  # where the credential came from ("" when not signed in)
    detail: str  # human context: plan, account, expiry — "" when unknown
    hint: str  # the exact sign-in step when not signed in ("" when signed in)
    # Machine-readable liveness, so a headless operator can ALERT rather than wait for
    # a failed job to be the first signal (#2549). The numbers were always right there
    # — `_anthropic_status` read `expires_at` and `_codex_status` called
    # `_jwt_is_expiring`; both then reduced the answer to a prose fragment and dropped
    # it. `None` means genuinely unknown (an opaque env token), never "fine".
    expires_at: float | None = None  # epoch seconds for the ACCESS token
    refreshable: bool = False  # a refresh token is present and we will use it
    durability: str = ""  # one of DURABILITY_* ("" when signed out)

    def as_dict(self) -> dict:
        return asdict(self)


# Both hints used to lead with the vendor CLI, and the Claude one offered
# CLAUDE_CODE_OAUTH_TOKEN as the co-equal alternative — which is the one path
# protoAgent NEVER refreshes and cannot inspect, so it reads green until it 401s.
# For a container that was the natural-looking option, i.e. the guidance steered
# away from the durable answer toward the brittle one (#2549). The operator-API
# sign-in leads now: it is headless-friendly by construction (Codex is a device
# code; the Claude PKCE flow displays a code rather than needing a redirect
# listener), and the credential lands in our own refreshed store.
_SIGN_IN_HINTS = {
    "anthropic-oauth": 'POST /api/config/oauth/start {"provider":"anthropic-oauth"} and '
    "approve on any device — the credential lands in protoAgent's own refreshed store. "
    "In the console: Settings → Model → Connected account. Signing in with the Claude "
    "Code CLI (`claude`) also works. CLAUDE_CODE_OAUTH_TOKEN is a bootstrap/override "
    "only — it is never refreshed.",
    "openai-codex": 'POST /api/config/oauth/start {"provider":"openai-codex"} and enter '
    "the device code on any device — the credential lands in protoAgent's own refreshed "
    "store. In the console: Settings → Model → Connected account. Signing in with the "
    "Codex CLI (`codex`) also works; protoAgent imports that credential once and keeps "
    "its own refreshed copy.",
}


def _epoch(value: object, *, scale: float = 1.0) -> float | None:
    """A timestamp we can actually publish, or None. Never a guess."""
    return float(value) / scale if isinstance(value, (int, float)) else None


def _jwt_expiry(access_token: str) -> float | None:
    """The ``exp`` claim of a JWT access token, or None if it isn't readable.

    ``_jwt_is_expiring`` already parses this and throws the number away. Read-only and
    total: a malformed token yields None (unknown), never an exception — a status poll
    must never be the thing that breaks."""
    if not isinstance(access_token, str) or access_token.count(".") != 2:
        return None
    try:
        return _epoch(_oauth._b64url_json(access_token.split(".")[1]).get("exp"))
    except Exception:  # noqa: BLE001 — an unparseable token is "unknown", not an error
        return None


def _anthropic_status() -> OAuthStatus:
    if os.environ.get(_oauth._CLAUDE_ENV_VAR, "").strip():
        # An opaque string we cannot inspect and never refresh: no expiry to report,
        # and `static` so a dashboard doesn't read it as equivalent to a live login.
        return OAuthStatus(
            "anthropic-oauth",
            True,
            "env",
            "CLAUDE_CODE_OAUTH_TOKEN (not refreshed — set once, replace by hand)",
            "",
            durability=DURABILITY_STATIC,
        )
    store = _oauth._read_anthropic_store()
    if store:
        exp = _epoch(store.get("expires_at"))
        refreshable = bool(str(store.get("refresh_token", "") or "").strip())
        detail = "Claude subscription (signed in here)"
        if exp is not None and exp <= _oauth._now():
            detail += " (token will refresh on use)"
        return OAuthStatus(
            "anthropic-oauth",
            True,
            "instance_store",
            detail,
            "",
            expires_at=exp,
            refreshable=refreshable,
            durability=DURABILITY_MANAGED if refreshable else DURABILITY_STATIC,
        )
    # File on Linux/WSL; the Keychain item on macOS — same document shape.
    for source, doc in (
        ("credentials_file", _oauth._read_claude_credentials_file()),
        ("keychain", _oauth._read_claude_keychain()),
    ):
        oauth = (doc or {}).get("claudeAiOauth") if isinstance(doc, dict) else None
        if isinstance(oauth, dict) and str(oauth.get("accessToken", "") or "").strip():
            plan = str(oauth.get("subscriptionType", "") or "").strip()
            detail = f"{plan} plan" if plan else "Claude Code credentials"
            # The CLI's own document, in milliseconds. We read it; we don't refresh it —
            # that stays the CLI's job, which is exactly what makes it `borrowed`.
            return OAuthStatus(
                "anthropic-oauth",
                True,
                source,
                detail,
                "",
                expires_at=_epoch(oauth.get("expiresAt"), scale=1000.0),
                refreshable=False,
                durability=DURABILITY_BORROWED,
            )
    return OAuthStatus("anthropic-oauth", False, "", "", _SIGN_IN_HINTS["anthropic-oauth"])


def _codex_status() -> OAuthStatus:
    """Read-only: is a Codex token present (our store or the CLI file), unexpired?
    Never refreshes or writes — a status poll must be side-effect-free."""
    from infra.paths import instance_paths

    store = _oauth._codex_store_path(instance_paths())
    tokens = _oauth._read_codex_tokens(store)
    source = "instance_store"
    if tokens is None:
        tokens = _oauth._read_codex_tokens(_oauth._CODEX_CLI_AUTH_FILE)
        source = "codex_cli"
    if not tokens or not str(tokens.get("access_token", "") or "").strip():
        return OAuthStatus("openai-codex", False, "", "", _SIGN_IN_HINTS["openai-codex"])
    acct = _oauth._codex_account_id(tokens)
    expiring = _oauth._jwt_is_expiring(str(tokens["access_token"]), 0)
    detail = "ChatGPT account" + (f" …{acct[-6:]}" if acct else "")
    if expiring:
        # Token itself is stale but the refresh token is likely still good — we'll
        # refresh transparently on first use, so still "signed in".
        detail += " (token will refresh on use)"
    refreshable = bool(str(tokens.get("refresh_token", "") or "").strip())
    return OAuthStatus(
        "openai-codex",
        True,
        source,
        detail,
        "",
        expires_at=_jwt_expiry(str(tokens["access_token"])),
        refreshable=refreshable,
        durability=(DURABILITY_MANAGED if source == "instance_store" and refreshable else DURABILITY_BORROWED),
    )


def oauth_status(provider: str) -> OAuthStatus:
    """Read-only sign-in status for a native OAuth provider."""
    provider = (provider or "").strip().lower()
    if provider not in ("anthropic-oauth", "openai-codex"):
        raise ValueError(f"not a native OAuth provider: {provider!r}")
    # An explicit disconnect (#2440) reads as signed-out even if a vendor CLI credential
    # is still on disk — otherwise status would say "signed in" while resolution refuses.
    if _oauth.is_disconnected(provider):
        return OAuthStatus(provider, False, "", "disconnected", _SIGN_IN_HINTS[provider])
    return _anthropic_status() if provider == "anthropic-oauth" else _codex_status()


def all_oauth_status() -> list[dict]:
    """Status for every native OAuth provider — the wizard renders all of them."""
    return [oauth_status(p).as_dict() for p in sorted(NATIVE_OAUTH_PROVIDERS)]


# ── model listing ────────────────────────────────────────────────────────────

# Fallback Claude ids if the live /models probe fails (offline, or the OAuth token
# can't list). Kept short and current; the live probe is preferred.
_ANTHROPIC_FALLBACK_MODELS = [
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]
_MODELS_TIMEOUT_S = 15.0


def _list_codex_models(config: "LangGraphConfig") -> tuple[list[str], str]:
    try:
        creds = _oauth.resolve_codex_oauth()
    except _oauth.OAuthCredentialError as exc:
        return [], str(exc)
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "User-Agent": "codex-cli",
        "originator": "codex_cli_rs",
    }
    if creds.account_id:
        headers["ChatGPT-Account-Id"] = creds.account_id
    try:
        resp = httpx.get(
            f"{creds.base_url}/models?client_version=1.0.0",
            headers=headers,
            timeout=_MODELS_TIMEOUT_S,
        )
        resp.raise_for_status()
        models = [str(m.get("slug")) for m in resp.json().get("models", []) if isinstance(m, dict) and m.get("slug")]
        return models, ""
    except httpx.HTTPError as exc:
        return [], f"Could not list Codex models: {exc}"


def _list_anthropic_models() -> tuple[list[str], str]:
    try:
        creds = _oauth.resolve_anthropic_oauth()
    except _oauth.OAuthCredentialError as exc:
        return _ANTHROPIC_FALLBACK_MODELS, str(exc)
    from graph.providers.anthropic_oauth import oauth_default_headers

    headers = {"Authorization": f"Bearer {creds.access_token}", "anthropic-version": "2023-06-01"}
    headers.update(oauth_default_headers())
    try:
        resp = httpx.get("https://api.anthropic.com/v1/models", headers=headers, timeout=_MODELS_TIMEOUT_S)
        resp.raise_for_status()
        models = [str(m.get("id")) for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        return (models or _ANTHROPIC_FALLBACK_MODELS), ""
    except httpx.HTTPError:
        # The OAuth token may not carry models:list scope — fall back to the curated set.
        return _ANTHROPIC_FALLBACK_MODELS, ""


def list_provider_models(provider: str, config: "LangGraphConfig") -> tuple[list[str], str]:
    """Return ``(models, error)`` for a native OAuth provider's account.

    Codex is probed live from the account's ``/models`` endpoint; Claude tries the
    Anthropic ``/v1/models`` API and falls back to a curated list.
    """
    provider = (provider or "").strip().lower()
    if provider == "openai-codex":
        return _list_codex_models(config)
    if provider == "anthropic-oauth":
        return _list_anthropic_models()
    raise ValueError(f"not a native OAuth provider: {provider!r}")


def validate_oauth_connection(provider: str, model: str, config: "LangGraphConfig") -> tuple[bool, str]:
    """The wizard/Settings "Test connection" for a native OAuth provider.

    Builds the real client and streams a 1-token turn (Codex requires streaming and
    forbids a system message, so we send a bare user prompt). Returns ``(ok, error)``.
    """
    provider = (provider or "").strip().lower()
    if provider not in NATIVE_OAUTH_PROVIDERS:
        return False, f"not a native OAuth provider: {provider!r}"
    try:
        from langchain_core.messages import HumanMessage

        from graph.providers import build_native_oauth_llm

        llm = build_native_oauth_llm(provider, config, model_name=(model or "").strip() or None)
        got = False
        for _chunk in llm.stream([HumanMessage("Reply with: ok")]):
            got = True
            break
        if not got:
            return False, "The provider accepted the request but streamed no response."
        return True, ""
    except _oauth.OAuthCredentialError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — surface the provider's own error text
        return False, str(exc)
