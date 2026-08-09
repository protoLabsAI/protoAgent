"""Native OAuth-subscription providers (ADR 0097).

Covers the create_llm dispatch, the two client builders, credential resolution
(Claude read-live + Codex bootstrap/refresh/own), and the Claude Code identity
middleware. The one thing these can't cover is a real subscription round-trip —
that's the draft-PR live gate.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from graph.config import LangGraphConfig
from graph.llm import create_llm
from graph.providers import (
    NATIVE_OAUTH_PROVIDERS,
    build_native_oauth_llm,
    is_native_oauth_provider,
)
from graph.providers import oauth as oauth_mod
from graph.providers.oauth import (
    CodexOAuthCreds,
    OAuthCredentialError,
    resolve_anthropic_oauth,
    resolve_codex_oauth,
)


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "PROTOAGENT_CODEX_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _jwt(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{seg}.s"


# ── dispatch ──────────────────────────────────────────────────────────────────


def test_provider_set_membership():
    assert NATIVE_OAUTH_PROVIDERS == {"anthropic-oauth", "openai-codex"}
    assert is_native_oauth_provider("anthropic-oauth")
    assert is_native_oauth_provider("OpenAI-Codex")  # case-insensitive
    assert not is_native_oauth_provider("openai")
    assert not is_native_oauth_provider("")
    assert not is_native_oauth_provider(None)


def test_gateway_path_is_untouched_for_default_provider():
    from langchain_openai import ChatOpenAI

    cfg = LangGraphConfig(model_provider="openai", api_key="k", model_name="protolabs/reasoning")
    llm = create_llm(cfg)
    assert isinstance(llm, ChatOpenAI)


def test_build_native_oauth_llm_rejects_unknown():
    with pytest.raises(ValueError, match="not a native OAuth provider"):
        build_native_oauth_llm("openai", LangGraphConfig())


def test_headless_setup_exempts_native_oauth_from_api_key():
    """A native OAuth provider needs no api_base/api_key to pass setup validation —
    it authenticates from a credential store (mirrors the ACP exemption)."""
    from graph.config_io import validate_for_headless

    # gateway provider with no key → blocked
    ok, _ = validate_for_headless(LangGraphConfig(model_provider="openai", api_base="", api_key=""))
    assert not ok
    # native OAuth provider with no key/base → allowed
    ok, reason = validate_for_headless(
        LangGraphConfig(model_provider="openai-codex", api_base="", api_key="")
    )
    assert ok, reason
    ok, reason = validate_for_headless(
        LangGraphConfig(model_provider="anthropic-oauth", api_base="", api_key="")
    )
    assert ok, reason


# ── anthropic-oauth ─────────────────────────────────────────────────────────────


def test_anthropic_oauth_reads_env_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-ENVTOKEN")
    creds = resolve_anthropic_oauth()
    assert creds.access_token == "cc-ENVTOKEN"
    assert creds.source == "env"


def test_anthropic_oauth_reads_credentials_file(monkeypatch, tmp_path):
    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "cc-FILE", "expiresAt": (time.time() + 3600) * 1000}}))
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", cred)
    creds = resolve_anthropic_oauth()
    assert creds.access_token == "cc-FILE"
    assert creds.source == "credentials_file"


def test_anthropic_oauth_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "nope.json")
    with pytest.raises(OAuthCredentialError) as ei:
        resolve_anthropic_oauth()
    assert ei.value.provider == "anthropic-oauth"


def test_create_llm_anthropic_oauth_uses_bearer(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-BEARER")
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-sonnet-4-5")
    llm = create_llm(cfg)
    assert type(llm).__name__ == "_OAuthChatAnthropic"
    # The load-bearing guard: api_key swapped for auth_token in the SDK client params.
    params = llm._client_params
    assert "api_key" not in params
    assert params["auth_token"] == "cc-BEARER"
    headers = {k.lower(): v for k, v in llm._client.default_headers.items()}
    assert headers["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert headers["user-agent"].startswith("claude-code/")
    assert "x-api-key" not in {k.lower() for k in llm._client.default_headers}


def test_anthropic_oauth_rejects_gateway_alias(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")
    cfg = LangGraphConfig(model_provider="anthropic-oauth", model_name="protolabs/reasoning")
    with pytest.raises(RuntimeError, match="not a Claude model id"):
        create_llm(cfg)


# ── openai-codex ────────────────────────────────────────────────────────────────


def test_codex_account_id_from_explicit_and_jwt():
    assert oauth_mod._codex_account_id({"account_id": "acct-explicit"}) == "acct-explicit"
    jwt = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-claim"}})
    assert oauth_mod._codex_account_id({"id_token": jwt}) == "acct-claim"
    assert oauth_mod._codex_account_id({"access_token": "not-a-jwt"}) is None


def test_codex_jwt_expiry():
    fresh = _jwt({"exp": time.time() + 3600})
    stale = _jwt({"exp": time.time() - 10})
    assert not oauth_mod._jwt_is_expiring(fresh, 120)
    assert oauth_mod._jwt_is_expiring(stale, 120)
    assert oauth_mod._jwt_is_expiring("garbage", 120)


def test_codex_bootstrap_from_cli_and_own_copy(monkeypatch, tmp_path):
    """First resolve imports ~/.codex/auth.json, then keeps its own instance copy."""
    cli = tmp_path / "codex_auth.json"
    fresh = _jwt({"exp": time.time() + 3600})
    cli.write_text(json.dumps({"tokens": {"access_token": fresh, "refresh_token": "r", "account_id": "acct-1"}}))
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)

    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)

    creds = resolve_codex_oauth(paths=object())
    assert creds.account_id == "acct-1"
    assert creds.source == "codex_cli_bootstrap"
    assert store.exists()  # our own copy was written
    # Second call reads our store, not the CLI file.
    creds2 = resolve_codex_oauth(paths=object())
    assert creds2.source == "instance_store"


def test_codex_refresh_on_expiry(monkeypatch, tmp_path):
    stale = _jwt({"exp": time.time() - 10})
    fresh = _jwt({"exp": time.time() + 3600})
    store = tmp_path / "codex-oauth.json"
    store.write_text(json.dumps({"tokens": {"access_token": stale, "refresh_token": "r-old", "account_id": "acct-9"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)

    calls = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": fresh, "refresh_token": "r-new"}

    def _fake_post(url, **kw):
        calls["url"] = url
        calls["data"] = kw.get("data")
        return _Resp()

    monkeypatch.setattr(oauth_mod.httpx, "post", _fake_post)
    creds = resolve_codex_oauth(paths=object())
    assert creds.access_token == fresh
    assert calls["data"]["grant_type"] == "refresh_token"
    # Rotated refresh token is persisted.
    assert json.loads(store.read_text())["tokens"]["refresh_token"] == "r-new"


def test_codex_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "store-nope.json")
    with pytest.raises(OAuthCredentialError) as ei:
        resolve_codex_oauth(paths=object())
    assert ei.value.provider == "openai-codex"


def test_create_llm_codex_responses_config(monkeypatch):
    import graph.providers.openai_codex as ocx

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(
            access_token="cdx-TOK", account_id="acct-7",
            base_url="https://chatgpt.com/backend-api/codex", source="instance_store",
        ),
    )
    cfg = LangGraphConfig(model_provider="openai-codex", model_name="gpt-5-codex", reasoning_effort="high")
    llm = create_llm(cfg)
    assert llm.use_responses_api is True
    assert llm.store is False
    assert llm.include == ["reasoning.encrypted_content"]
    assert llm.reasoning == {"effort": "high", "summary": "auto"}
    headers = llm.default_headers or {}
    assert headers["ChatGPT-Account-Id"] == "acct-7"
    assert headers["originator"] == "codex_cli_rs"


def test_codex_rejects_gateway_alias(monkeypatch):
    import graph.providers.openai_codex as ocx

    monkeypatch.setattr(
        ocx, "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(access_token="t", account_id="a", base_url="b", source="s"),
    )
    cfg = LangGraphConfig(model_provider="openai-codex", model_name="protolabs/reasoning")
    with pytest.raises(RuntimeError, match="not an OpenAI model id"):
        create_llm(cfg)


# ── claude code identity middleware ─────────────────────────────────────────────


class _FakeReq:
    def __init__(self, sysmsg):
        self.system_message = sysmsg

    def override(self, **kw):
        self.system_message = kw.get("system_message", self.system_message)
        return self


def _mw():
    from graph.middleware.claude_code_identity import ClaudeCodeIdentityMiddleware

    return ClaudeCodeIdentityMiddleware()


def test_identity_prefixes_string_system():
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    req = _mw()._transform(_FakeReq(SystemMessage(content="You are Aria.")))
    assert req.system_message.content.startswith(CLAUDE_CODE_SYSTEM_PREFIX)


def test_identity_is_idempotent():
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    mw = _mw()
    once = mw._transform(_FakeReq(SystemMessage(content="You are Aria.")))
    twice = mw._transform(once)
    assert twice.system_message.content.count(CLAUDE_CODE_SYSTEM_PREFIX) == 1


def test_identity_prepends_block_list():
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    blocks = [{"type": "text", "text": "You are Aria.", "cache_control": {"type": "ephemeral"}}]
    req = _mw()._transform(_FakeReq(SystemMessage(content=blocks)))
    content = req.system_message.content
    assert len(content) == 2
    assert content[0]["text"] == CLAUDE_CODE_SYSTEM_PREFIX


# ── codex responses-input middleware ────────────────────────────────────────────


class _FakeCodexReq:
    def __init__(self, sysmsg, model):
        self.system_message = sysmsg
        self.model = model

    def override(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        return self


def test_codex_moves_system_to_instructions():
    """The Codex backend forbids system-role items; the middleware moves the system
    prompt to a bound `instructions` kwarg and clears the system message."""
    from langchain_core.messages import SystemMessage

    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    class _Model:
        def __init__(self):
            self.bound = None

        def bind(self, **kw):
            self.bound = kw
            return self

    model = _Model()
    # block-structured system (post-PromptCache) flattens to text
    sysmsg = SystemMessage(content=[{"type": "text", "text": "You are Aria."}, {"type": "text", "text": "Be terse."}])
    req = CodexResponsesInputMiddleware()._transform(_FakeCodexReq(sysmsg, model))
    assert req.system_message is None
    assert model.bound == {"instructions": "You are Aria.\n\nBe terse."}


def test_codex_middleware_noop_without_system():
    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    req = _FakeCodexReq(None, object())
    assert CodexResponsesInputMiddleware()._transform(req) is req
