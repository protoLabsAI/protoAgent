"""Infisical provider (ADR 0080 D3) — universal-auth machine identity over raw REST.

Two calls, no SDK: ``POST /api/v1/auth/universal-auth/login`` (client id/secret →
bearer token) and ``GET /api/v3/secrets/raw`` (the project/environment/path listing).
The v3-raw endpoint is deliberate: it is what the official Python SDK and the External
Secrets Operator call, so it works against old and new self-hosted servers alike; the
official ``infisicalsdk`` is skipped because it unconditionally drags boto3+botocore
(frozen-build weight) and does no token renewal either way.

Token handling: the access token is cached per ``(host, client_id)`` and re-minted at
80% of its advertised ``expiresIn`` — or immediately on a 401 (revoked/rotated
mid-TTL), with a single retry. Secret values never appear in error strings.
"""

from __future__ import annotations

import hashlib
import threading
import time

import httpx

from infra.secrets.base import ErrorKind, FetchResult, SecretsProvider, SourceConfig

DEFAULT_HOST = "https://us.infisical.com"


class InfisicalProvider(SecretsProvider):
    name = "infisical"
    bootstrap_env = ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET")

    def __init__(self, transport: httpx.BaseTransport | None = None):
        # ``transport`` is a test seam (httpx.MockTransport) — None in production.
        self._transport = transport
        self._lock = threading.Lock()
        self._tokens: dict[tuple, tuple[str, float]] = {}  # _cache_key(cfg) → (token, refresh_after)

    # -- internals ----------------------------------------------------------------

    def _host(self, cfg: SourceConfig) -> str:
        return (cfg.host or DEFAULT_HOST).rstrip("/")

    def _cache_key(self, cfg: SourceConfig) -> tuple:
        # The client_secret fingerprint is part of the identity: without it, a cached
        # token would keep serving after the secret is rotated OR make a connection
        # test with wrong credentials false-positive (the smoke caught exactly that).
        secret_fp = hashlib.sha256(cfg.client_secret.encode()).hexdigest()[:16]
        return (self._host(cfg), cfg.client_id, secret_fp)

    def _login(self, client: httpx.Client, cfg: SourceConfig) -> str:
        r = client.post(
            f"{self._host(cfg)}/api/v1/auth/universal-auth/login",
            json={"clientId": cfg.client_id, "clientSecret": cfg.client_secret},
        )
        if r.status_code != 200:
            raise _LoginRejected(r.status_code)
        body = r.json()
        token = str(body.get("accessToken") or "")
        if not token:
            raise _LoginRejected(200)
        expires_in = float(body.get("expiresIn") or 3600)
        with self._lock:
            self._tokens[self._cache_key(cfg)] = (token, time.monotonic() + expires_in * 0.8)
        return token

    def _token(self, client: httpx.Client, cfg: SourceConfig) -> str:
        with self._lock:
            cached = self._tokens.get(self._cache_key(cfg))
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        return self._login(client, cfg)

    def _drop_token(self, cfg: SourceConfig) -> None:
        with self._lock:
            self._tokens.pop(self._cache_key(cfg), None)

    def _list(self, client: httpx.Client, cfg: SourceConfig, token: str) -> httpx.Response:
        return client.get(
            f"{self._host(cfg)}/api/v3/secrets/raw",
            params={
                "workspaceId": cfg.project_id,
                "environment": cfg.environment,
                "secretPath": cfg.path or "/",
                "recursive": "true" if cfg.recursive else "false",
                "expandSecretReferences": "true",
                "include_imports": "true",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    # -- SecretsProvider ------------------------------------------------------------

    def fetch(self, cfg: SourceConfig) -> FetchResult:
        if not cfg.project_id:
            return FetchResult(error="secrets_manager.project_id is not set", error_kind=ErrorKind.NOT_CONFIGURED)
        if not cfg.client_id or not cfg.client_secret:
            return FetchResult(
                error=(
                    "missing machine-identity credentials — set client_id/client_secret "
                    "(secrets.yaml or INFISICAL_CLIENT_ID / INFISICAL_CLIENT_SECRET env)"
                ),
                error_kind=ErrorKind.NOT_CONFIGURED,
            )
        try:
            with httpx.Client(timeout=cfg.timeout_seconds, transport=self._transport) as client:
                token = self._token(client, cfg)
                r = self._list(client, cfg, token)
                if r.status_code == 401:
                    # Token revoked/expired mid-TTL — re-login once, then retry.
                    self._drop_token(cfg)
                    token = self._login(client, cfg)
                    r = self._list(client, cfg, token)
                if r.status_code != 200:
                    kind = ErrorKind.AUTH_FAILED if r.status_code in (401, 403) else ErrorKind.BAD_RESPONSE
                    return FetchResult(error=f"list secrets: HTTP {r.status_code}", error_kind=kind)
                return FetchResult(values=_merge_listing(r.json()))
        except _LoginRejected as e:
            kind = ErrorKind.AUTH_FAILED if e.status in (400, 401, 403) else ErrorKind.BAD_RESPONSE
            return FetchResult(error=f"universal-auth login: HTTP {e.status}", error_kind=kind)
        except httpx.TimeoutException:
            return FetchResult(
                error=f"timed out after {cfg.timeout_seconds:g}s talking to {self._host(cfg)}",
                error_kind=ErrorKind.TIMEOUT,
            )
        except httpx.HTTPError as e:
            return FetchResult(error=f"network error: {e.__class__.__name__}: {e}", error_kind=ErrorKind.NETWORK)
        except (ValueError, KeyError, TypeError) as e:
            return FetchResult(error=f"unexpected response shape: {e}", error_kind=ErrorKind.BAD_RESPONSE)
        except Exception as e:  # noqa: BLE001 — the contract is never-raise
            return FetchResult(error=f"{e.__class__.__name__}: {e}", error_kind=ErrorKind.INTERNAL)


class _LoginRejected(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


def _merge_listing(body: dict) -> dict[str, str]:
    """Flatten a v3-raw listing. Imports merge first (lower precedence), then the
    path's own secrets — matching Infisical's documented resolution order."""
    values: dict[str, str] = {}
    for imp in body.get("imports") or []:
        for s in (imp or {}).get("secrets") or []:
            _put(values, s)
    for s in body.get("secrets") or []:
        _put(values, s)
    return values


def _put(values: dict[str, str], secret: dict) -> None:
    key = str((secret or {}).get("secretKey") or "")
    if key:
        values[key] = str(secret.get("secretValue") or "")
