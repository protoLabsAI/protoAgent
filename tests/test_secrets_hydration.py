"""External secrets-manager hydration (ADR 0080) — infra/secrets + the from_yaml hook.

Covers the orchestrator's apply policy (existing-env-wins, ownership, protected vars,
removals, TTL/fingerprint gating, required escalation), the Infisical provider over an
httpx.MockTransport (login → v3-raw list, imports merge, 401 re-login, timeout), the
from_yaml wiring (hydrate-before-parse, inert when disabled), and redaction pickup.
"""

from __future__ import annotations

import json
import textwrap

import httpx
import pytest

import infra.secrets as ext
from infra.secrets import (
    ErrorKind,
    FetchResult,
    InfisicalProvider,
    SecretsProvider,
    SecretsRequiredError,
    SourceConfig,
    hydrate_from_docs,
    register_secrets_provider,
)
from infra.secrets.hydrate import _reset_for_tests


class FakeProvider(SecretsProvider):
    name = "fake"
    bootstrap_env = ("FAKE_TOKEN",)

    def __init__(self):
        self.calls = 0
        self.values: dict[str, str] = {}
        self.result: FetchResult | None = None  # explicit failure override

    def fetch(self, cfg: SourceConfig) -> FetchResult:
        self.calls += 1
        if self.result is not None:
            return self.result
        return FetchResult(values=dict(self.values))


@pytest.fixture()
def fake(monkeypatch):
    """A registered fake provider + clean orchestrator state, torn down after."""
    provider = FakeProvider()
    register_secrets_provider(provider)
    _reset_for_tests()
    monkeypatch.setenv("FAKE_TOKEN", "bootstrap-cred")
    yield provider
    _reset_for_tests()
    ext.base._PROVIDERS.pop("fake", None)


def _docs(**sm) -> tuple[dict, dict]:
    section = {"enabled": True, "provider": "fake", "project_id": "p1", **sm}
    return {"secrets_manager": section}, {}


# ---------------------------------------------------------------------------
# Orchestrator apply policy
# ---------------------------------------------------------------------------


def test_apply_sets_owns_and_registers_redaction(fake, monkeypatch):
    monkeypatch.delenv("HYDRATED_KEY", raising=False)
    fake.values = {"HYDRATED_KEY": "sk-manager-value-123"}
    status = hydrate_from_docs(*_docs())
    assert status is not None and status.ok
    import os

    assert os.environ["HYDRATED_KEY"] == "sk-manager-value-123"
    assert ext.applied_env_names() == ["HYDRATED_KEY"]
    assert "sk-manager-value-123" in ext.sensitive_values()


def test_preexisting_env_shadows_by_default(fake, monkeypatch):
    monkeypatch.setenv("SHADOWED_KEY", "operator-set")
    fake.values = {"SHADOWED_KEY": "manager-value"}
    status = hydrate_from_docs(*_docs())
    import os

    assert os.environ["SHADOWED_KEY"] == "operator-set"
    assert status.shadowed == ["SHADOWED_KEY"]
    assert ext.applied_env_names() == []


def test_override_env_prefers_the_manager(fake, monkeypatch):
    monkeypatch.setenv("SHADOWED_KEY", "operator-set")
    fake.values = {"SHADOWED_KEY": "manager-value"}
    hydrate_from_docs(*_docs(override_env=True))
    import os

    assert os.environ["SHADOWED_KEY"] == "manager-value"
    assert ext.applied_env_names() == ["SHADOWED_KEY"]


def test_bootstrap_and_identity_vars_are_protected(fake, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")
    fake.values = {
        "FAKE_TOKEN": "evil-overwrite",
        "PROTOAGENT_INSTANCE": "prod",
        "OK_VAR": "value-12345",
    }
    hydrate_from_docs(*_docs(override_env=True))
    import os

    assert os.environ["FAKE_TOKEN"] == "bootstrap-cred"
    assert os.environ["PROTOAGENT_INSTANCE"] == "dev"
    assert os.environ["OK_VAR"] == "value-12345"


def test_invalid_names_and_blank_values_skipped(fake, monkeypatch):
    monkeypatch.delenv("GOOD_ONE", raising=False)
    fake.values = {"not a var": "x", "BLANK": "", "GOOD_ONE": "fine-value"}
    hydrate_from_docs(*_docs())
    import os

    assert os.environ["GOOD_ONE"] == "fine-value"
    assert ext.applied_env_names() == ["GOOD_ONE"]
    assert "not a var" not in os.environ and "BLANK" not in os.environ


def test_refresh_updates_and_removes_owned_only(fake, monkeypatch):
    import os

    monkeypatch.delenv("ROTATES", raising=False)
    monkeypatch.delenv("GOES_AWAY", raising=False)
    fake.values = {"ROTATES": "value-one-1", "GOES_AWAY": "temp-value-1"}
    hydrate_from_docs(*_docs())
    assert os.environ["GOES_AWAY"] == "temp-value-1"

    # Rotation: one value changes, one disappears from the manager.
    fake.values = {"ROTATES": "value-two-2"}
    hydrate_from_docs(*_docs(), force=True)
    assert os.environ["ROTATES"] == "value-two-2"
    assert "GOES_AWAY" not in os.environ
    assert ext.applied_env_names() == ["ROTATES"]

    # A var the operator overwrote since we set it is theirs — removal skips the
    # env write but drops ownership.
    fake.values = {"ROTATES": "value-two-2", "GOES_AWAY": "temp-value-2"}
    hydrate_from_docs(*_docs(), force=True)
    os.environ["GOES_AWAY"] = "operator-took-over"
    fake.values = {"ROTATES": "value-two-2"}
    hydrate_from_docs(*_docs(), force=True)
    assert os.environ["GOES_AWAY"] == "operator-took-over"
    assert ext.applied_env_names() == ["ROTATES"]


def test_ttl_gate_dedups_and_force_bypasses(fake):
    fake.values = {"SOME_VAR": "value-123"}
    hydrate_from_docs(*_docs())
    hydrate_from_docs(*_docs())  # same fingerprint, inside the window → no fetch
    assert fake.calls == 1
    hydrate_from_docs(*_docs(), force=True)
    assert fake.calls == 2
    # A config change (fingerprint) also bypasses the window.
    hydrate_from_docs(*_docs(environment="staging"))
    assert fake.calls == 3


def test_failure_warns_and_continues_by_default(fake, caplog):
    fake.result = FetchResult(error="boom", error_kind=ErrorKind.NETWORK)
    with caplog.at_level("WARNING", logger="protoagent.secrets"):
        status = hydrate_from_docs(*_docs())
    assert status is not None and not status.ok and status.error_kind == "network"
    assert any("fetch failed" in r.message for r in caplog.records)


def test_required_failure_raises(fake):
    fake.result = FetchResult(error="down", error_kind=ErrorKind.NETWORK)
    with pytest.raises(SecretsRequiredError):
        hydrate_from_docs(*_docs(required=True))


def test_unknown_provider_is_a_contained_error(fake):
    merged = {"secrets_manager": {"enabled": True, "provider": "nope", "project_id": "p"}}
    status = hydrate_from_docs(merged, {})
    assert status is not None and not status.ok
    assert status.error_kind == "not_configured"


def test_disable_env_escape_hatch(fake, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_NO_SECRETS_HYDRATE", "1")
    assert hydrate_from_docs(*_docs()) is None
    assert fake.calls == 0


def test_bootstrap_creds_resolve_secrets_yaml_first(fake):
    captured: dict = {}

    real_fetch = fake.fetch

    def spy(cfg):
        captured["cfg"] = cfg
        return real_fetch(cfg)

    fake.fetch = spy
    merged, _ = _docs(client_id="from-yaml")
    secrets_doc = {"secrets_manager": {"client_id": "from-secrets-yaml", "client_secret": "s3cret"}}
    hydrate_from_docs(merged, secrets_doc, force=True)
    assert captured["cfg"].client_id == "from-secrets-yaml"
    assert captured["cfg"].client_secret == "s3cret"


# ---------------------------------------------------------------------------
# from_yaml wiring — hydrate before parse, inert when absent/disabled
# ---------------------------------------------------------------------------


def _write_config(tmp_path, body: str):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_from_yaml_hydrates_env_before_parse(fake, tmp_path, monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake.values = {"OPENAI_API_KEY": "sk-from-manager-1"}
    p = _write_config(
        tmp_path,
        """
        model:
          api_base: http://gateway:4000/v1
        secrets_manager:
          enabled: true
          provider: fake
          project_id: p1
        """,
    )
    cfg = LangGraphConfig.from_yaml(p)
    import os

    assert os.environ["OPENAI_API_KEY"] == "sk-from-manager-1"
    # The config attr stays blank (nothing in yaml/secrets.yaml) — the documented
    # lazy env fallback in graph/llm.py picks the hydrated var up per call.
    assert cfg.api_key == ""
    assert cfg.secrets_manager_enabled is True
    assert cfg.secrets_manager_provider == "fake"


def test_from_yaml_disabled_section_never_fetches(fake, tmp_path, monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    p = _write_config(
        tmp_path,
        """
        secrets_manager:
          enabled: false
          provider: fake
        """,
    )
    LangGraphConfig.from_yaml(p)
    assert fake.calls == 0


def test_from_yaml_required_failure_propagates(fake, tmp_path, monkeypatch):
    from graph.config import LangGraphConfig

    monkeypatch.setattr("graph.config._resolve_plugin_config", lambda *a, **k: {})
    monkeypatch.setattr("graph.config._load_host_layer", lambda: {})
    fake.result = FetchResult(error="down", error_kind=ErrorKind.NETWORK)
    p = _write_config(
        tmp_path,
        """
        secrets_manager:
          enabled: true
          provider: fake
          project_id: p1
          required: true
        """,
    )
    with pytest.raises(SecretsRequiredError):
        LangGraphConfig.from_yaml(p)


# ---------------------------------------------------------------------------
# Redaction pickup (ADR 0080 D7)
# ---------------------------------------------------------------------------


def test_redaction_scrubs_manager_values(fake, monkeypatch):
    monkeypatch.delenv("WEIRD_SHAPE_CRED", raising=False)
    fake.values = {"WEIRD_SHAPE_CRED": "zZ9!totally-unpatterned-cred"}
    hydrate_from_docs(*_docs())
    from graph.middleware.redaction import redact

    assert redact("error calling api with zZ9!totally-unpatterned-cred oops") == (
        "error calling api with [REDACTED] oops"
    )


# ---------------------------------------------------------------------------
# Infisical provider over httpx.MockTransport
# ---------------------------------------------------------------------------


def _infisical_cfg(**kw) -> SourceConfig:
    base = dict(
        provider="infisical",
        host="https://infisical.test",
        project_id="proj-1",
        environment="prod",
        path="/agent",
        client_id="cid",
        client_secret="csec",
        timeout_seconds=5.0,
    )
    base.update(kw)
    return SourceConfig(**base)


def _mock_provider(handler) -> InfisicalProvider:
    return InfisicalProvider(transport=httpx.MockTransport(handler))


def test_infisical_happy_path_merges_imports_lower_precedence():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            body = json.loads(request.content)
            seen["login"] = body
            return httpx.Response(200, json={"accessToken": "tok-1", "expiresIn": 3600})
        assert request.url.path == "/api/v3/secrets/raw"
        seen["params"] = dict(request.url.params)
        assert request.headers["Authorization"] == "Bearer tok-1"
        return httpx.Response(
            200,
            json={
                "secrets": [{"secretKey": "OPENAI_API_KEY", "secretValue": "sk-path-wins"}],
                "imports": [
                    {
                        "secrets": [
                            {"secretKey": "OPENAI_API_KEY", "secretValue": "sk-imported"},
                            {"secretKey": "EXTRA", "secretValue": "extra-value"},
                        ]
                    }
                ],
            },
        )

    result = _mock_provider(handler).fetch(_infisical_cfg())
    assert result.ok
    assert result.values == {"OPENAI_API_KEY": "sk-path-wins", "EXTRA": "extra-value"}
    assert seen["login"] == {"clientId": "cid", "clientSecret": "csec"}
    assert seen["params"]["workspaceId"] == "proj-1"
    assert seen["params"]["environment"] == "prod"
    assert seen["params"]["secretPath"] == "/agent"
    assert seen["params"]["expandSecretReferences"] == "true"


def test_infisical_relogins_once_on_401():
    calls = {"login": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            calls["login"] += 1
            return httpx.Response(200, json={"accessToken": f"tok-{calls['login']}", "expiresIn": 3600})
        calls["list"] += 1
        if request.headers["Authorization"] == "Bearer tok-1":
            return httpx.Response(401)
        return httpx.Response(200, json={"secrets": [{"secretKey": "K", "secretValue": "v-123456"}]})

    provider = _mock_provider(handler)
    result = provider.fetch(_infisical_cfg())
    assert result.ok and result.values == {"K": "v-123456"}
    assert calls == {"login": 2, "list": 2}


def test_infisical_login_rejected_is_auth_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad identity"})

    result = _mock_provider(handler).fetch(_infisical_cfg())
    assert not result.ok and result.error_kind == ErrorKind.AUTH_FAILED
    assert "csec" not in result.error  # never leak the credential


def test_infisical_timeout_maps_to_timeout_kind():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    result = _mock_provider(handler).fetch(_infisical_cfg())
    assert not result.ok and result.error_kind == ErrorKind.TIMEOUT


def test_infisical_unconfigured_short_circuits():
    result = InfisicalProvider().fetch(_infisical_cfg(client_secret=""))
    assert not result.ok and result.error_kind == ErrorKind.NOT_CONFIGURED
    result = InfisicalProvider().fetch(_infisical_cfg(project_id=""))
    assert not result.ok and result.error_kind == ErrorKind.NOT_CONFIGURED


def test_infisical_token_cached_across_fetches():
    calls = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            calls["login"] += 1
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        return httpx.Response(200, json={"secrets": []})

    provider = _mock_provider(handler)
    assert provider.fetch(_infisical_cfg()).ok
    assert provider.fetch(_infisical_cfg()).ok
    assert calls["login"] == 1


def test_infisical_changed_secret_does_not_reuse_cached_token():
    """A cached token must not mask wrong/rotated credentials (the live smoke caught
    a connection test false-positive here): the cache key includes a client_secret
    fingerprint, so different credentials always re-login."""
    calls = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            calls["login"] += 1
            body = json.loads(request.content)
            if body["clientSecret"] != "csec":
                return httpx.Response(401)
            return httpx.Response(200, json={"accessToken": "tok", "expiresIn": 3600})
        return httpx.Response(200, json={"secrets": []})

    provider = _mock_provider(handler)
    assert provider.fetch(_infisical_cfg()).ok  # warms the cache for the good secret
    bad = provider.fetch(_infisical_cfg(client_secret="wrong"))
    assert not bad.ok and bad.error_kind == ErrorKind.AUTH_FAILED
    assert calls["login"] == 2
