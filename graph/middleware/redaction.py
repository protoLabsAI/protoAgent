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
# Two invariants, in priority order:
#
# 1. LEAK-FREE. A match never leaves part of a secret visible. Narrowing a value class
#    to "stop at the first comma" turned `API_KEY=abc,def` into `API_KEY=[REDACTED],def`
#    — a partial leak, worse than any mangled delimiter.
# 2. SYNTAX-PRESERVING (#3816, #4166). Redaction runs over tool output before the model
#    reads it (AuditMiddleware), so a match that swallows the closing quote after a token
#    hands a code reviewer source that no longer parses: `"Authorization: Bearer <jwt>",`
#    became `"Authorization: Bearer [REDACTED]` and the review panel FAILed
#    projectBoard-plugin#523 on a SyntaxError that wasn't on disk.
#
# Both hold by making the CONTEXTUAL patterns (an Authorization header, a `KEY=value`
# assignment, `api_key: …`, `client_secret: …`) quote-aware: the regex below matches
# only the PREFIX, and `_value_end` decides where the value stops —
#   * inside a quoted string (the value's own opening quote, or the quote the whole
#     `KEY=value` / header text sits in): everything up to the MATCHING closing quote on
#     that line, and the quote is kept — so `"KEY=a,b;c d",` → `"KEY=[REDACTED]",`;
#   * unquoted: up to whitespace, a quote character, or a closing bracket `) ] } >` —
#     never at `, ; & ( [ { <`, which can sit inside a secret.
# Token SHAPES (sk-, ghp_, npm_, a JWT, …) match their provider's exact alphabet, which
# cannot contain a quote or bracket.

# The unquoted value: anything up to whitespace, a quote, or a closing bracket.
_UNQUOTED_STOP = re.compile(r"[\s\"'`)\]}>]")
# A bare template/variable reference (`${TOKEN}`, `$TOKEN`, `{token}`, `{{ secrets.X }}`)
# in the value position is not a secret — leave it, so `f"Authorization: Bearer {tok}"`
# and `Authorization: Bearer ${TOKEN}` read as written.
_TEMPLATE_REF = re.compile(r"\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*|\{\{\s*[\w.]+\s*\}\}|\{[A-Za-z_][\w.]*\}")

# An unquoted value that is a call/subscript on a dotted name whose argument opens with a
# quote or closes at once (`os.environ.get('X')`, `os.environ["X"]`, `getenv()`) is code
# reading a secret, not one: redacting `os.environ.get(` would leave `[REDACTED]'X')`, the
# unparseable artefact this module exists to avoid. (An unquoted secret could never
# carry the quote that follows anyway — the unquoted scan stops there.)
_CODE_REF = re.compile(r"[A-Za-z_][\w.]*[(\[][\"'`)\]]")

_ENV_KEYS = (
    r"OPENAI_API_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_PUBLIC_KEY|"
    r"A2A_AUTH_TOKEN|API_KEY|SECRET_KEY|PUBLIC_KEY|AUTH_TOKEN|ACCESS_TOKEN|"
    r"PRIVATE_KEY|DISCORD_BOT_TOKEN|BOT_TOKEN|GATEWAY_API_KEY|GH_PAT|"
    r"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AWS_ACCESS_KEY_ID|NPM_TOKEN|"
    r"CLIENT_SECRET|INFISICAL_CLIENT_SECRET"
)

PATTERNS: dict[str, re.Pattern] = {
    # Contextual PREFIXES — each ends where the value begins; `_redact_values` finds the
    # value's end. `q1` = a quote immediately before the key (the whole thing sits in a
    # string), `sep` = the separator, whose trailing quote (if any) opens the value.
    #
    # `Authorization: Bearer x`, the JSON/dict spelling (`"Authorization": "Bearer x"`),
    # and the Basic/token schemes.
    "bearer_token": re.compile(
        r"(?i)(?P<q1>[\"'`])?\bAuthorization(?P<sep>[\"'`]?\s*[:=]\s*[\"'`]?)(?:Bearer|Basic|token)[ \t]+",
    ),
    # `PUBLIC_KEY` bare, not just `LANGFUSE_PUBLIC_KEY`: #3017 gave that key a config
    # home, and `secrets.yaml` spells it `public_key`, so a tool that echoes the file
    # would leak it while its `secret_key` sibling one line down was scrubbed. The
    # leading `\b` keeps the bare alternative from matching inside a longer name.
    "env_var_assignment": re.compile(
        r"(?i)(?P<q1>[\"'`])?\b(?P<key>" + _ENV_KEYS + r")(?P<sep>[\"'`]?[ \t]*[=:][ \t]*[\"'`]?)",
    ),
    # Value must open with a 16+ char key-shaped run (keeps `api_key = os.environ[…]`
    # alone); once it does, the WHOLE value goes, not just that run.
    "generic_api_key": re.compile(
        r"(?i)(?P<q1>[\"'`])?(?:api[_\-]?key|apikey)(?P<sep>[\"'\s:=]+)",
    ),
    "client_secret": re.compile(
        r"(?i)(?P<q1>[\"'`])?client[_\-]?secret(?P<sep>[\"'\s:=]+)",
    ),
    # Token shapes — the provider's exact alphabet, so a match is the secret and only it.
    "openai_key": re.compile(
        r"\bsk-[A-Za-z0-9_\-]{20,}\b",
    ),
    "google_oauth": re.compile(r"\bya29\.[A-Za-z0-9_\-]+"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    # npm tokens are base64url — `_` and `-` included.
    "npm_token": re.compile(r"\bnpm_[A-Za-z0-9_\-]{36,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    # AKIA = long-lived, ASIA = temporary (STS) access key ids.
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "discord_token": re.compile(r"\b[MNO][A-Za-z0-9_\-]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b"),
    # The password half of URL userinfo (`https://user:pass@host`) — group 1 keeps the
    # scheme and user; the password runs to the LAST `@` before the host (a raw `@` in a
    # password must not leave its tail visible), and the `@host` stays.
    "url_userinfo": re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'`<>]+:)(?!\[REDACTED\]@)[^\s/\"'`<>]+(?=@)"),
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
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "NPM_TOKEN",
        # Header spelling, as a dict key (`{"X-Api-Key": …}`).
        "X_API_KEY",
    }
)


def _normalize_key(key: str) -> str:
    """Spelling-insensitive key form: casefolded, `-` and `_` dropped — so `ApiKey`,
    `apiKey`, `Api-Key` and `API_KEY` are one key. Membership is exact on the result,
    so `tokenizer`, `author` and `keyboard` stay ordinary keys."""
    return key.casefold().replace("-", "").replace("_", "")


_SENSITIVE_KEYS_NORMALIZED: frozenset[str] = frozenset(_normalize_key(k) for k in _SENSITIVE_ENV_KEYS)


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and _normalize_key(key) in _SENSITIVE_KEYS_NORMALIZED


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


_QUOTES = "\"'`"


def _active_quote(m: re.Match) -> str | None:
    """The quote the value sits in, if any. A quote ending the separator opens the value
    (`"KEY": "v"`); otherwise a quote right before the key wraps the whole text
    (`"KEY=v"`) — unless the separator already closed it (`'KEY': v`)."""
    sep = m.group("sep") or ""
    if sep and sep[-1] in _QUOTES:
        return sep[-1]
    q1 = m.group("q1")
    if q1 and not any(c in _QUOTES for c in sep):
        return q1
    return None


def _value_end(s: str, start: int, quote: str | None) -> int:
    """Where the value starting at ``start`` ends (exclusive). Quoted: at the matching,
    unescaped closing quote on the same line (the quote itself survives). Unquoted — or
    a quote that never closes on this line — at whitespace, a quote, or `) ] } >`."""
    if quote:
        i, n = start, len(s)
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == quote:
                return i
            if c == "\n":
                break
            i += 1
    m = _UNQUOTED_STOP.search(s, start)
    return m.start() if m else len(s)


def _redact_values(s: str, prefix: re.Pattern, min_run: int = 0) -> str:
    """Replace the value after each ``prefix`` match with the placeholder, quote-aware.

    ``min_run`` > 0: only when the value opens with that many key-shaped characters
    (the heuristic patterns' false-positive guard) — but then the whole value goes."""
    out: list[str] = []
    pos = 0
    for m in prefix.finditer(s):
        if m.start() < pos:
            continue
        start = m.end()
        quote = _active_quote(m)
        if s.startswith(_PLACEHOLDER, start):
            # Already redacted by an earlier pattern: only a tail it left behind goes.
            if _value_end(s, start + len(_PLACEHOLDER), quote) == start + len(_PLACEHOLDER):
                continue
        end = _value_end(s, start, quote)
        span = s[start:end]
        if not span or (quote is None and _CODE_REF.match(s, start)):
            continue
        # A bare variable reference standing alone as the value (its `}` may be what
        # stopped the unquoted scan) is code, not a credential.
        ref = _TEMPLATE_REF.match(s, start)
        if ref and (ref.end() >= end) and (ref.end() == len(s) or _UNQUOTED_STOP.match(s, ref.end())):
            continue
        if min_run and not re.match(r"[A-Za-z0-9_\-]{%d,}" % min_run, span):
            continue
        out.append(s[pos:start])
        out.append(_PLACEHOLDER)
        pos = end
    if not out:
        return s
    out.append(s[pos:])
    return "".join(out)


def _redact_string_simple(value: str) -> str:
    """Contextual values first (quote-aware), then token shapes, then exact values."""
    value = _redact_values(value, PATTERNS["bearer_token"])
    value = _redact_values(value, PATTERNS["env_var_assignment"])
    value = _redact_values(value, PATTERNS["generic_api_key"], min_run=16)
    value = _redact_values(value, PATTERNS["client_secret"], min_run=12)

    # Provider token shapes — full-match redaction.
    for _name in (
        "jwt",
        "openai_key",
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
            if _is_sensitive_key(k):
                result[k] = _PLACEHOLDER
            else:
                result[k] = redact(v, _depth + 1)
        return result

    if isinstance(data, list):
        return [redact(item, _depth + 1) for item in data]

    if isinstance(data, tuple):
        return tuple(redact(item, _depth + 1) for item in data)

    return data
