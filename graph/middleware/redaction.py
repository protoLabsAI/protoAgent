"""Credential redaction for audit logs and Langfuse spans.

Provides a redact() function that scrubs sensitive credentials from
strings and nested data structures before they are written to audit.jsonl
or emitted as Langfuse observations.
"""

from __future__ import annotations

import re
from typing import Any

# Patterns are compiled once at module load for performance.
#
# FORMAT-PRESERVING (#3816, and the bearer/delimiter follow-up): a match covers the
# secret's own characters and NOTHING after them. Redaction runs over tool output
# before the model reads it (AuditMiddleware), so a pattern that swallows a closing
# quote, bracket, comma or semicolon hands a code reviewer source that no longer
# parses — `"Authorization: Bearer <jwt>",` became `"Authorization: Bearer [REDACTED]`
# and the review panel FAILed a PR on a SyntaxError that wasn't on disk
# (projectBoard-plugin#523). So every value class below either is a token's exact
# alphabet or stops at `_VALUE_STOP`; never `\S+`.
_VALUE_STOP = "\\s\"'`()\\[\\]{}<>,;&"
# A free-form secret value: one or more characters up to the first whitespace,
# quote, bracket, or list/statement separator. A secret that itself contains one of
# those characters is redacted only up to it — the price of keeping the
# surrounding syntax intact (exact-match scrubbing of manager-sourced values,
# below, still covers those whole).
_VALUE = f"[^{_VALUE_STOP}]+"
# RFC 6750 b64token — the whole alphabet a Bearer/Basic credential can use.
_B64TOKEN = r"[A-Za-z0-9\-._~+/]+=*"

PATTERNS: dict[str, re.Pattern] = {
    # `Authorization: Bearer x`, plus the JSON/dict spelling (`"Authorization":
    # "Bearer x"`) and the Basic/token schemes. The credential is RFC 6750's
    # b64token alphabet, so it can never run into a closing quote or comma.
    "bearer_token": re.compile(
        r"(Authorization[\"']?\s*[:=]\s*[\"']?(?:Bearer|Basic|token)\s+)" + _B64TOKEN,
        re.IGNORECASE,
    ),
    "openai_key": re.compile(
        r"\bsk-[A-Za-z0-9_\-]{20,}\b",
    ),
    "generic_api_key": re.compile(
        r"(?i)(?:api[_\-]?key|apikey)(?:[\"'\s:=]+)([A-Za-z0-9_\-]{16,})",
    ),
    # Provider token shapes a tool error / payload might echo into the audit log.
    "google_oauth": re.compile(r"\bya29\.[A-Za-z0-9_\-]+"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    "npm_token": re.compile(r"\bnpm_[A-Za-z0-9]{36,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "discord_token": re.compile(r"\b[MNO][A-Za-z0-9_\-]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b"),
    # The password half of URL userinfo (`https://user:pass@host`) — group 1 keeps
    # the scheme and user, the `@` stays.
    "url_userinfo": re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'`<>]+:)(?!\[REDACTED\])[^\s/@\"'`<>]+(?=@)"),
    "client_secret": re.compile(
        r"(?i)client[_\-]?secret(?:[\"'\s:=]+)([A-Za-z0-9_\-]{12,})",
    ),
    # `PUBLIC_KEY` bare, not just `LANGFUSE_PUBLIC_KEY`: #3017 gave that key a config
    # home, and `secrets.yaml` spells it `public_key`, so a tool that echoes the file
    # would leak it while its `secret_key` sibling one line down was scrubbed. The
    # leading `\b` keeps the bare alternative from matching inside a longer name.
    "env_var_assignment": re.compile(
        r"(?i)\b(OPENAI_API_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_PUBLIC_KEY|"
        r"A2A_AUTH_TOKEN|API_KEY|SECRET_KEY|PUBLIC_KEY|AUTH_TOKEN|ACCESS_TOKEN|"
        r"PRIVATE_KEY|DISCORD_BOT_TOKEN|BOT_TOKEN|GATEWAY_API_KEY|GH_PAT|"
        r"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|NPM_TOKEN|"
        r"CLIENT_SECRET|INFISICAL_CLIENT_SECRET)\s*[=:]\s*"
        # The value, format-preserving (#3816): an optional opening quote, then the
        # value up to `_VALUE_STOP`. Already-redacted values (an earlier pattern got
        # there first) are left alone rather than re-wrapped.
        r"[\"'`]?(?!\[REDACTED\])" + _VALUE,
    ),
}

# Known environment variable key names whose dict values should be redacted.
_SENSITIVE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "A2A_AUTH_TOKEN",
        "API_KEY",
        "SECRET_KEY",
        # Langfuse's "public" key is only public relative to their browser SDK; server-side
        # it authenticates the ingest client, which is why #3017 declares BOTH halves in
        # config_io.SECRET_PATHS. Redacted here (and as `public_key` below) so the audit
        # layer doesn't undo that call.
        "PUBLIC_KEY",
        "AUTH_TOKEN",
        "ACCESS_TOKEN",
        "PRIVATE_KEY",
        "authorization",
        "Authorization",
        "api_key",
        "apikey",
        "secret_key",
        "public_key",
        "auth_token",
        "access_token",
        "private_key",
        "DISCORD_BOT_TOKEN",
        "bot_token",
        "GATEWAY_API_KEY",
        "GH_PAT",
        "client_secret",
        "CLIENT_SECRET",
        "refresh_token",
        "REFRESH_TOKEN",
        "INFISICAL_CLIENT_SECRET",
    }
)

_PLACEHOLDER = "[REDACTED]"
_MAX_DEPTH = 10


def _known_secret_values() -> frozenset[str]:
    """Exact secret values pulled by the external secrets manager (ADR 0080 D7) —
    manager-sourced credentials have arbitrary shapes the patterns above can't know,
    so they're scrubbed by exact match. Lazy + guarded: redaction must keep working
    (fail open on the patterns, never crash) even if the secrets subsystem doesn't."""
    try:
        from infra.secrets import sensitive_values

        return sensitive_values()
    except Exception:  # noqa: BLE001 — never let redaction itself raise
        return frozenset()


def _redact_string_simple(value: str) -> str:
    """Simplified string redaction using direct substitution."""
    # Bearer token
    value = PATTERNS["bearer_token"].sub(r"\1[REDACTED]", value)
    # OpenAI-style key
    value = PATTERNS["openai_key"].sub(_PLACEHOLDER, value)

    # Generic api_key — redact the captured credential value group
    def _replace_api_key(m: re.Match) -> str:
        full = m.group(0)
        cred = m.group(1)
        return full[: len(full) - len(cred)] + _PLACEHOLDER

    value = PATTERNS["generic_api_key"].sub(_replace_api_key, value)

    # env var assignment — keep the key name, redact value after =/:
    def _replace_env_var(m: re.Match) -> str:
        full = m.group(0)
        key = m.group(1)
        # find the separator and everything after
        rest = full[len(key) :]
        sep_match = re.match(r"\s*[=:]\s*[\"'`]?", rest)  # keep an opening quote too
        if sep_match:
            return key + sep_match.group(0) + _PLACEHOLDER
        return key + _PLACEHOLDER

    value = PATTERNS["env_var_assignment"].sub(_replace_env_var, value)

    # Provider token shapes — full-match redaction.
    for _name in (
        "jwt",
        "google_oauth",
        "google_api_key",
        "github_token",
        "github_pat",
        "npm_token",
        "slack_token",
        "aws_access_key",
        "discord_token",
    ):
        value = PATTERNS[_name].sub(_PLACEHOLDER, value)

    # URL userinfo — keep scheme://user:, redact the password, keep the @host.
    value = PATTERNS["url_userinfo"].sub(r"\1[REDACTED]", value)

    # client_secret — redact the captured credential value group.
    def _replace_client_secret(m: re.Match) -> str:
        full, cred = m.group(0), m.group(1)
        return full[: len(full) - len(cred)] + _PLACEHOLDER

    value = PATTERNS["client_secret"].sub(_replace_client_secret, value)

    # Manager-sourced values (ADR 0080) — exact-match scrub, shape-agnostic.
    for secret_value in _known_secret_values():
        if secret_value in value:
            value = value.replace(secret_value, _PLACEHOLDER)
    return value


def redact(data: Any, _depth: int = 0) -> Any:
    """Recursively redact credentials from strings, dicts, and lists.

    Args:
        data: The value to redact. May be a string, dict, list, or any other type.

    Returns:
        A copy of ``data`` with all credential patterns replaced by [REDACTED].
        Non-string scalar values (int, bool, None, etc.) are returned unchanged.
    """
    if _depth > _MAX_DEPTH:
        # Fail CLOSED past the depth guard. Returning the subtree as-is let a credential
        # nested deep enough reach an audit record or trace unredacted.
        return _PLACEHOLDER

    if isinstance(data, str):
        return _redact_string_simple(data)

    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for k, v in data.items():
            # Redact values for known sensitive keys unconditionally
            if k in _SENSITIVE_ENV_KEYS or (isinstance(k, str) and k.upper() in _SENSITIVE_ENV_KEYS):
                result[k] = _PLACEHOLDER
            else:
                result[k] = redact(v, _depth + 1)
        return result

    if isinstance(data, list):
        return [redact(item, _depth + 1) for item in data]

    if isinstance(data, tuple):
        return tuple(redact(item, _depth + 1) for item in data)

    return data
