"""External secrets-manager integration (ADR 0080).

Pulls secrets from a configured manager (Infisical built-in) into ``os.environ``
before the config parse, so the documented env fallback tier — gateway key, A2A
token, plugin ``requires_env``, MCP/delegate child env — sees manager values on
every load path, plus a refresh loop for rotation. See ``hydrate`` for the apply
policy and ``base`` for the provider contract.

Note: this package is ``infra.secrets``; the stdlib ``secrets`` module is unaffected
(absolute imports) — just don't ``from infra import secrets`` in a module that also
wants the stdlib one.
"""

from infra.secrets.base import (
    ENV_NAME_RE,
    ErrorKind,
    FetchResult,
    SecretsProvider,
    SourceConfig,
    get_provider,
    register_secrets_provider,
)
from infra.secrets.hydrate import (
    DISABLE_ENV,
    SecretsRequiredError,
    SourceStatus,
    applied_env_names,
    hydrate_from_docs,
    sensitive_values,
    source_from_docs,
    status,
)
from infra.secrets.infisical import InfisicalProvider

# The built-in provider registers at import — config load resolves providers through
# the registry only, so a fork can replace this with its own registration.
register_secrets_provider(InfisicalProvider())

__all__ = [
    "DISABLE_ENV",
    "ENV_NAME_RE",
    "ErrorKind",
    "FetchResult",
    "InfisicalProvider",
    "SecretsProvider",
    "SecretsRequiredError",
    "SourceConfig",
    "SourceStatus",
    "applied_env_names",
    "get_provider",
    "hydrate_from_docs",
    "register_secrets_provider",
    "sensitive_values",
    "source_from_docs",
    "status",
]
