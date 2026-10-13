"""External secrets-manager provider contract + registry (ADR 0080).

A provider turns a :class:`SourceConfig` into a flat ``{ENV_NAME: value}`` mapping.
The contract is deliberately narrow (hermes-agent-informed):

- **Providers fetch; the orchestrator applies.** A provider returns the mapping it
  *would* contribute. Env-name validation, protected vars, existing-env precedence,
  ownership tracking, and every ``os.environ`` write live in :mod:`infra.secrets.hydrate`
  so no backend can get policy wrong.
- **Never raises, never prompts.** All failures come back as a :class:`FetchResult`
  with a typed :class:`ErrorKind` and a one-line, secret-free ``error`` string —
  hydration runs inside config load, where an exception would take the boot down.
- Synchronous and bounded: a provider honors ``SourceConfig.timeout_seconds`` for all
  network I/O.

Additional providers register via :func:`register_secrets_provider`. This is core-level
extensibility (a new provider is a small module here); a *plugin*-contributed provider
would need a pre-config discovery seam that does not exist — see ADR 0080 non-goals.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ErrorKind(str, Enum):
    """Why a fetch failed — stable vocabulary for logs, status API, and the console."""

    NOT_CONFIGURED = "not_configured"  # missing project/credentials/unknown provider
    AUTH_FAILED = "auth_failed"  # login rejected / token rejected twice
    NETWORK = "network"  # connect/TLS/DNS-level failure
    TIMEOUT = "timeout"  # request exceeded timeout_seconds
    BAD_RESPONSE = "bad_response"  # non-2xx or unparseable body
    INTERNAL = "internal"  # provider bug — anything unexpected


@dataclass
class SourceConfig:
    """One configured secrets source (the flat ``secrets_manager`` config section).

    ``client_id``/``client_secret`` arrive already resolved (secrets.yaml → YAML → env)
    — providers never do credential lookup themselves.
    """

    provider: str = "infisical"
    host: str = ""
    project_id: str = ""
    environment: str = "prod"
    path: str = "/"
    recursive: bool = True
    timeout_seconds: float = 10.0
    client_id: str = ""
    client_secret: str = ""
    # Apply/refresh policy (orchestrator-owned, not fetch inputs):
    required: bool = False
    override_env: bool = False
    refresh_seconds: int = 300

    def fingerprint(self) -> str:
        """Identity of this source for the TTL gate: any change to a fetch- or
        apply-relevant field must force a refetch on the next load, while repeated
        loads of an unchanged config within the TTL stay off the network. Credentials
        are folded in hashed (a rotated bootstrap identity = a different source)."""
        material = "\x1f".join(
            [
                self.provider,
                self.host,
                self.project_id,
                self.environment,
                self.path,
                str(self.recursive),
                str(self.override_env),
                hashlib.sha256(f"{self.client_id}\x1f{self.client_secret}".encode()).hexdigest(),
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class FetchResult:
    """What a provider hands back: a mapping, or a typed one-line failure."""

    values: dict[str, str] | None = None
    error: str = ""
    error_kind: ErrorKind | None = None

    @property
    def ok(self) -> bool:
        return self.values is not None


class SecretsProvider:
    """Base class for secret-manager backends. Subclass, set ``name`` (the config
    ``secrets_manager.provider`` value) and ``bootstrap_env`` (the env-var names the
    provider's own credentials fall back to — the orchestrator also protects these
    from ever being overwritten by fetched values), and implement :meth:`fetch`."""

    name: str = ""
    bootstrap_env: tuple[str, ...] = ()

    def fetch(self, cfg: SourceConfig) -> FetchResult:  # pragma: no cover — interface
        raise NotImplementedError


_PROVIDERS: dict[str, SecretsProvider] = {}


def register_secrets_provider(provider: SecretsProvider) -> None:
    """Register a provider under its ``name``. Last registration wins (a fork can
    replace the built-in). Invalid providers are rejected loudly — this runs at
    import/registration time, not on the fetch path."""
    name = getattr(provider, "name", "") or ""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"secrets provider name must be a lowercase identifier, got {name!r}")
    if not callable(getattr(provider, "fetch", None)):
        raise ValueError(f"secrets provider {name!r} has no fetch()")
    _PROVIDERS[name] = provider


def get_provider(name: str) -> SecretsProvider | None:
    return _PROVIDERS.get(name)
