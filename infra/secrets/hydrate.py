"""Env-hydration orchestrator (ADR 0080 D1/D2/D5/D6).

The single owner of every ``os.environ`` write in the secrets subsystem. Providers
return mappings; this module decides what actually lands:

- **Existing env wins** — a var already present (and not previously set by us) is
  skipped unless ``override_env: true``. Manager-hydrated vars behave exactly like
  the documented env fallback tier, never above it.
- **Ownership/provenance** — every var we set is remembered. Refresh only ever
  updates or removes vars we own, so rotation can't clobber operator-set env.
- **Protected vars** — the provider's own bootstrap credentials and the
  ``PROTOAGENT_*`` instance identity can never be overwritten by fetched values.
- **Never breaks a load** — fetch failures warn and fall through to whatever the env
  already has; only ``required: true`` escalates (``SecretsRequiredError``), turning a
  missing manager into a fail-fast boot instead of a half-configured agent.
- **TTL gate** — back-to-back config loads (boot → validate → reload chains) reuse the
  last successful fetch; any change to the source config (fingerprint) or ``force=True``
  (the refresh loop / operator sync) goes back to the network. In-process only — no
  disk cache, by design.

Called from ``graph/config.py::from_yaml`` (before the dataclass parse) and from the
server refresh loop / operator routes with ``force=True``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from infra.secrets.base import (
    ENV_NAME_RE,
    ErrorKind,
    FetchResult,
    SourceConfig,
    get_provider,
)

log = logging.getLogger("protoagent.secrets")

# Vars no fetched value may ever overwrite, regardless of override_env: the instance
# identity (env-only by ADR 0004/0065 — a manager rewriting it mid-run would re-point
# every store) plus each provider's own bootstrap credentials (added dynamically).
PROTECTED_BASE = (
    "PROTOAGENT_HOME",
    "PROTOAGENT_BOX_ROOT",
    "PROTOAGENT_INSTANCE",
    "PROTOAGENT_PLUGINS_DIR",
    "PATH",
    "HOME",
    "PYTHONPATH",
)

# Env escape hatch: disable hydration entirely (debugging / emergency).
DISABLE_ENV = "PROTOAGENT_NO_SECRETS_HYDRATE"

# Failed fetches retry no sooner than this, so a down manager can't turn back-to-back
# config loads into a hammer; successful fetches gate on the refresh interval instead.
FAILURE_RETRY_SECONDS = 30.0


class SecretsRequiredError(RuntimeError):
    """A ``required: true`` source failed to fetch — the caller should fail fast."""


@dataclass
class SourceStatus:
    """Last outcome for the status API / console. Never carries secret values."""

    enabled: bool = False
    provider: str = ""
    host: str = ""
    project_id: str = ""
    environment: str = ""
    path: str = ""
    ok: bool = False
    error: str = ""
    error_kind: str = ""
    fetched_at: str = ""  # ISO-8601 UTC of the last *attempt*
    applied: int = 0  # vars currently owned by the orchestrator
    shadowed: list[str] = field(default_factory=list)  # skipped: pre-existing env won
    refresh_seconds: int = 0

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "host": self.host,
            "project_id": self.project_id,
            "environment": self.environment,
            "path": self.path,
            "ok": self.ok,
            "error": self.error,
            "error_kind": self.error_kind,
            "fetched_at": self.fetched_at,
            "applied": self.applied,
            "shadowed": list(self.shadowed),
            "refresh_seconds": self.refresh_seconds,
        }


_LOCK = threading.RLock()
_APPLIED: dict[str, str] = {}  # env var → value we set (ownership + change detection)
_SENSITIVE_VALUES: set[str] = set()  # applied values, for redaction (D7)
_LAST_STATUS: SourceStatus | None = None
_LAST_FINGERPRINT = ""
_LAST_ATTEMPT_MONO = 0.0
_LAST_OK = False


def source_from_docs(merged: dict | None, secrets_doc: dict | None) -> SourceConfig | None:
    """Build a :class:`SourceConfig` from the raw config + secrets docs, or ``None``
    when the section is absent/disabled. Bootstrap credentials resolve with the
    standard secret precedence: secrets.yaml → main YAML → env (the provider's
    ``bootstrap_env`` names)."""
    sm = (merged or {}).get("secrets_manager") or {}
    if not isinstance(sm, dict) or not sm.get("enabled"):
        return None
    sec = (secrets_doc or {}).get("secrets_manager") or {}
    if not isinstance(sec, dict):
        sec = {}
    provider_name = str(sm.get("provider") or "infisical").strip()
    provider = get_provider(provider_name)
    env_names = tuple(getattr(provider, "bootstrap_env", ()) or ())

    def _cred(key: str, env_index: int) -> str:
        v = sec.get(key) or sm.get(key)
        if v:
            return str(v)
        if env_index < len(env_names):
            return os.environ.get(env_names[env_index], "")
        return ""

    return SourceConfig(
        provider=provider_name,
        host=str(sm.get("host") or ""),
        project_id=str(sm.get("project_id") or ""),
        environment=str(sm.get("environment") or "prod"),
        path=str(sm.get("path") or "/"),
        recursive=bool(sm.get("recursive", True)),
        timeout_seconds=float(sm.get("timeout_seconds") or 10.0),
        client_id=_cred("client_id", 0),
        client_secret=_cred("client_secret", 1),
        required=bool(sm.get("required", False)),
        override_env=bool(sm.get("override_env", False)),
        refresh_seconds=int(sm.get("refresh_seconds", 300) or 0),
    )


def hydrate_from_docs(
    merged: dict | None, secrets_doc: dict | None, *, force: bool = False
) -> SourceStatus | None:
    """Fetch-and-apply for the configured source. Returns the resulting status, or
    ``None`` when hydration is disabled/unconfigured. Raises only
    :class:`SecretsRequiredError` (``required: true`` + failed fetch)."""
    if os.environ.get(DISABLE_ENV, "").strip():
        return None
    cfg = source_from_docs(merged, secrets_doc)
    if cfg is None:
        return None

    global _LAST_FINGERPRINT, _LAST_ATTEMPT_MONO, _LAST_STATUS, _LAST_OK
    with _LOCK:
        fp = cfg.fingerprint()
        if not force and fp == _LAST_FINGERPRINT and _LAST_STATUS is not None:
            window = (
                float(max(30, cfg.refresh_seconds)) if (_LAST_OK and cfg.refresh_seconds > 0) else 60.0
            )
            if not _LAST_OK:
                window = FAILURE_RETRY_SECONDS
            if (time.monotonic() - _LAST_ATTEMPT_MONO) < window:
                return _LAST_STATUS

        provider = get_provider(cfg.provider)
        if provider is None:
            result = FetchResult(
                error=f"unknown secrets provider {cfg.provider!r}", error_kind=ErrorKind.NOT_CONFIGURED
            )
        else:
            result = provider.fetch(cfg)
            if not isinstance(result, FetchResult):  # a misbehaving provider, contained
                result = FetchResult(
                    error=f"provider {cfg.provider!r} returned {type(result).__name__}",
                    error_kind=ErrorKind.INTERNAL,
                )

        status = SourceStatus(
            enabled=True,
            provider=cfg.provider,
            host=cfg.host,
            project_id=cfg.project_id,
            environment=cfg.environment,
            path=cfg.path,
            ok=result.ok,
            error=result.error,
            error_kind=result.error_kind.value if result.error_kind else "",
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            refresh_seconds=cfg.refresh_seconds,
        )
        if result.ok:
            protected = set(PROTECTED_BASE) | set(getattr(provider, "bootstrap_env", ()) or ())
            changed, shadowed = _apply(result.values or {}, cfg, protected)
            status.applied = len(_APPLIED)
            status.shadowed = shadowed
            if changed or shadowed:
                log.info(
                    "[secrets] %s: %d env var(s) applied (%d changed this pass, %d shadowed by "
                    "pre-existing env%s)",
                    cfg.provider,
                    len(_APPLIED),
                    changed,
                    len(shadowed),
                    " — set secrets_manager.override_env to prefer the manager" if shadowed else "",
                )
            else:
                log.debug("[secrets] %s: fetch ok, %d env var(s) current", cfg.provider, len(_APPLIED))
        else:
            status.applied = len(_APPLIED)
            log.warning(
                "[secrets] %s fetch failed (%s): %s — continuing with the existing environment",
                cfg.provider,
                status.error_kind or "error",
                result.error,
            )

        _LAST_FINGERPRINT = fp
        _LAST_ATTEMPT_MONO = time.monotonic()
        _LAST_OK = result.ok
        _LAST_STATUS = status

        if cfg.required and not result.ok:
            raise SecretsRequiredError(
                f"secrets_manager.required is set and the {cfg.provider} fetch failed "
                f"({status.error_kind or 'error'}): {result.error}"
            )
        return status


def _apply(values: dict[str, str], cfg: SourceConfig, protected: set[str]) -> tuple[int, list[str]]:
    """Reconcile fetched values into ``os.environ``. Returns ``(changed, shadowed)``.
    Caller holds ``_LOCK``."""
    changed = 0
    shadowed: list[str] = []
    fetched: set[str] = set()
    invalid = 0
    for name in sorted(values):
        value = values[name]
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            invalid += 1
            continue
        if name in protected:
            continue
        if not isinstance(value, str) or value == "":
            continue  # a blank secret can't be meaningfully exported
        fetched.add(name)
        owned = name in _APPLIED
        if not owned and name in os.environ and not cfg.override_env:
            shadowed.append(name)
            continue
        if os.environ.get(name) != value:
            os.environ[name] = value
            changed += 1
        _APPLIED[name] = value
        if len(value) >= 8:
            _SENSITIVE_VALUES.add(value)

    # A var we own that a *successful* fetch no longer returns was deleted/moved in the
    # manager — un-export it (unless the operator overwrote it since; then it's theirs).
    for name in [n for n in _APPLIED if n not in fetched]:
        if os.environ.get(name) == _APPLIED[name]:
            os.environ.pop(name, None)
            changed += 1
            log.info("[secrets] %s no longer in the manager — removed from the environment", name)
        del _APPLIED[name]

    if invalid:
        log.warning("[secrets] skipped %d secret(s) whose names are not valid env vars", invalid)
    return changed, shadowed


def status() -> dict:
    """Status surface for the operator API — safe (no secret values; owned var *names*
    only, which the operator-authed console needs to show provenance)."""
    with _LOCK:
        base = _LAST_STATUS.as_dict() if _LAST_STATUS is not None else SourceStatus().as_dict()
        base["vars"] = sorted(_APPLIED)
        return base


def applied_env_names() -> list[str]:
    with _LOCK:
        return sorted(_APPLIED)


def sensitive_values() -> frozenset[str]:
    """Applied secret values (≥ 8 chars) — consumed by graph/middleware/redaction.py
    so manager-sourced secrets are scrubbed from audit logs by exact match (D7)."""
    with _LOCK:
        return frozenset(_SENSITIVE_VALUES)


def _reset_for_tests() -> None:
    """Drop all module state AND un-export owned vars — test isolation only."""
    global _LAST_STATUS, _LAST_FINGERPRINT, _LAST_ATTEMPT_MONO, _LAST_OK
    with _LOCK:
        for name, value in list(_APPLIED.items()):
            if os.environ.get(name) == value:
                os.environ.pop(name, None)
        _APPLIED.clear()
        _SENSITIVE_VALUES.clear()
        _LAST_STATUS = None
        _LAST_FINGERPRINT = ""
        _LAST_ATTEMPT_MONO = 0.0
        _LAST_OK = False
