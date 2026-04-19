"""Credential redaction for audit logs and Langfuse spans.

Provides a redact() function that scrubs sensitive credentials from
strings and nested data structures before they are written to audit.jsonl
or emitted as Langfuse observations.
"""

from __future__ import annotations

import re
from typing import Any

# Patterns are compiled once at module load for performance.
# Each entry is (pattern_name, compiled_regex, replacement).
PATTERNS: dict[str, re.Pattern] = {
    "bearer_token": re.compile(
        r"(Authorization\s*:\s*Bearer\s+)\S+",
        re.IGNORECASE,
    ),
    "openai_key": re.compile(
        r"\bsk-[A-Za-z0-9_\-]{20,}\b",
    ),
    "generic_api_key": re.compile(
        r"(?i)(?:api[_\-]?key|apikey)(?:[\"'\s:=]+)([A-Za-z0-9_\-]{16,})",
    ),
    "env_var_assignment": re.compile(
        r"(?i)\b(OPENAI_API_KEY|LANGFUSE_SECRET_KEY|LANGFUSE_PUBLIC_KEY|"
        r"A2A_AUTH_TOKEN|API_KEY|SECRET_KEY|AUTH_TOKEN|ACCESS_TOKEN|"
        r"PRIVATE_KEY)\s*[=:]\s*\S+",
    ),
}

# Known environment variable key names whose dict values should be redacted.
_SENSITIVE_ENV_KEYS: frozenset[str] = frozenset({
    "OPENAI_API_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "A2A_AUTH_TOKEN",
    "API_KEY",
    "SECRET_KEY",
    "AUTH_TOKEN",
    "ACCESS_TOKEN",
    "PRIVATE_KEY",
    "authorization",
    "Authorization",
    "api_key",
    "apikey",
    "secret_key",
    "auth_token",
    "access_token",
    "private_key",
})

_PLACEHOLDER = "[REDACTED]"
_MAX_DEPTH = 10


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
        rest = full[len(key):]
        sep_match = re.match(r"\s*[=:]\s*", rest)
        if sep_match:
            return key + sep_match.group(0) + _PLACEHOLDER
        return key + _PLACEHOLDER

    value = PATTERNS["env_var_assignment"].sub(_replace_env_var, value)
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
        return data

    if isinstance(data, str):
        return _redact_string_simple(data)

    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for k, v in data.items():
            # Redact values for known sensitive keys unconditionally
            if k in _SENSITIVE_ENV_KEYS or (
                isinstance(k, str) and k.upper() in _SENSITIVE_ENV_KEYS
            ):
                result[k] = _PLACEHOLDER
            else:
                result[k] = redact(v, _depth + 1)
        return result

    if isinstance(data, list):
        return [redact(item, _depth + 1) for item in data]

    if isinstance(data, tuple):
        return tuple(redact(item, _depth + 1) for item in data)

    return data
