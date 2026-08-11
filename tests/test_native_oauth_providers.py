"""Native OAuth-subscription providers (ADR 0097).

Covers the create_llm dispatch, the two client builders, credential resolution
(Claude read-live + Codex bootstrap/refresh/own), and the Claude Code identity
middleware. The one thing these can't cover is a real subscription round-trip —
that's the draft-PR live gate.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import types

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


_REAL_KEYCHAIN_READ = oauth_mod._read_claude_keychain


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "PROTOAGENT_CODEX_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # The DEV MACHINE's real Keychain holds a live Claude Code login — without this
    # stub, every "no credential anywhere" test passes/fails by which laptop runs it.
    # Tests that want the keychain re-patch it explicitly.
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", lambda: None)


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
    ok, reason = validate_for_headless(LangGraphConfig(model_provider="openai-codex", api_base="", api_key=""))
    assert ok, reason
    ok, reason = validate_for_headless(LangGraphConfig(model_provider="anthropic-oauth", api_base="", api_key=""))
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


def test_anthropic_oauth_omits_temperature(monkeypatch):
    """The current Claude models reject `temperature` ("deprecated for this model"),
    so anthropic-oauth must NOT forward config.temperature."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")
    from graph.providers.anthropic_oauth import build_anthropic_oauth_llm

    llm = build_anthropic_oauth_llm(
        LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5", temperature=0.2)
    )
    assert llm.temperature is None  # not the config's 0.2


def test_anthropic_oauth_empty_token_fails_clearly(monkeypatch):
    """A malformed store (empty token) must fail with a clear message, not the SDK's
    confusing "No api key passed in" 401."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    import graph.providers.anthropic_oauth as ao
    from graph.providers.oauth import AnthropicOAuthCreds

    monkeypatch.setattr(ao, "resolve_anthropic_oauth", lambda: AnthropicOAuthCreds("", "instance_store"))
    with pytest.raises(RuntimeError, match="empty access token"):
        ao.build_anthropic_oauth_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5"))


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

    creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert creds.account_id == "acct-1"
    assert creds.source == "codex_cli_bootstrap"
    assert store.exists()  # our own copy was written
    # Second call reads our store, not the CLI file.
    creds2 = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
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
    creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert creds.access_token == fresh
    assert calls["data"]["grant_type"] == "refresh_token"
    # Rotated refresh token is persisted.
    assert json.loads(store.read_text())["tokens"]["refresh_token"] == "r-new"


def test_codex_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "store-nope.json")
    with pytest.raises(OAuthCredentialError) as ei:
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert ei.value.provider == "openai-codex"


def test_create_llm_codex_responses_config(monkeypatch):
    import graph.providers.openai_codex as ocx

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(
            access_token="cdx-TOK",
            account_id="acct-7",
            base_url="https://chatgpt.com/backend-api/codex",
            source="instance_store",
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
        ocx,
        "resolve_codex_oauth",
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
        self.model_settings = {}

    def override(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        return self


def test_codex_moves_system_to_instructions():
    """The Codex backend forbids system-role items; the middleware moves the system
    prompt into `model_settings["instructions"]` and clears the system message. It
    must NOT ride a model binding: the agent factory re-binds tools from the raw
    model, which silently dropped bound kwargs — the #2519 no-persona bug."""
    from langchain_core.messages import SystemMessage

    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    model = object()  # the model must pass through untouched — no bind()
    # block-structured system (post-PromptCache) flattens to text
    sysmsg = SystemMessage(content=[{"type": "text", "text": "You are Aria."}, {"type": "text", "text": "Be terse."}])
    req = CodexResponsesInputMiddleware()._transform(_FakeCodexReq(sysmsg, model))
    assert req.system_message is None
    assert req.model is model
    assert req.model_settings["instructions"] == "You are Aria.\n\nBe terse."


def test_codex_instructions_survive_factory_tool_rebind():
    """Regression for #2519: the factory executes
    `request.model.bind_tools(tools, tool_choice=..., **request.model_settings)`.
    With instructions delivered as a model BINDING, that re-bind resolved
    bind_tools on the raw model (RunnableBinding.__getattr__) and produced a fresh
    binding WITHOUT the kwarg — every tool-bearing Codex turn shipped with no
    system prompt of any kind, while View prompt (captured upstream) looked right.
    This test drives the REAL ChatOpenAI + the factory's exact bind call down to
    the Responses payload, so the drop can never come back green."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    model = ChatOpenAI(model="gpt-5-codex", api_key="x", use_responses_api=True, output_version="v0")
    req = CodexResponsesInputMiddleware()._transform(_FakeCodexReq(SystemMessage(content="You are Aria."), model))
    tools = [
        {
            "type": "function",
            "function": {"name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    # The factory's standard-binding branch, verbatim shape.
    bound = req.model.bind_tools(tools, tool_choice=None, **req.model_settings)
    payload = model._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert payload.get("instructions") == "You are Aria."
    # And the input carries no system-role item (the Codex backend rejects those).
    assert all(item.get("role") != "system" for item in payload.get("input", []) if isinstance(item, dict))


def test_codex_middleware_noop_without_system():
    from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

    req = _FakeCodexReq(None, object())
    assert CodexResponsesInputMiddleware()._transform(req) is req


# ── discovery (console read-layer) ──────────────────────────────────────────────


def test_oauth_status_not_signed_in(monkeypatch, tmp_path):
    from graph.providers import discovery

    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "none.json")
    s = discovery.oauth_status("anthropic-oauth")
    assert s.provider == "anthropic-oauth"
    assert s.signed_in is False
    assert "claude" in s.hint.lower()


def test_oauth_status_signed_in_via_env(monkeypatch):
    from graph.providers import discovery

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")
    s = discovery.oauth_status("anthropic-oauth")
    assert s.signed_in is True
    assert s.source == "env"
    assert s.hint == ""


def test_all_oauth_status_covers_every_provider(monkeypatch, tmp_path):
    from graph.providers import discovery

    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "store.json")
    rows = discovery.all_oauth_status()
    assert {r["provider"] for r in rows} == {"anthropic-oauth", "openai-codex"}
    assert all(
        set(r) == {"provider", "signed_in", "source", "detail", "hint", "expires_at", "refreshable", "durability"}
        for r in rows
    )


# ── credential liveness is machine-readable (#2549) ───────────────────────────
def test_status_publishes_expiry_and_durability_for_our_own_store(monkeypatch, tmp_path):
    """A fleet operator's only question is "how long until this agent stops working,
    and will it fix itself?" — `expires_at` was read and then folded into prose."""
    from graph.providers import discovery

    store = tmp_path / "anthropic-oauth.json"
    exp = time.time() + 3600
    store.write_text(json.dumps({"access_token": "at", "refresh_token": "rt", "expires_at": exp}))
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: store)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    s = discovery.oauth_status("anthropic-oauth")

    assert s.signed_in is True and s.source == "instance_store"
    assert s.expires_at == pytest.approx(exp)
    assert s.refreshable is True
    assert s.durability == discovery.DURABILITY_MANAGED


def test_status_marks_a_borrowed_cli_login_and_converts_its_millis(monkeypatch, tmp_path):
    """The CLI's document is OURS to read, not to refresh — that is what makes it
    borrowed, and it stores expiry in milliseconds."""
    from graph.providers import discovery

    exp_ms = (time.time() + 1800) * 1000
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "at", "subscriptionType": "max", "expiresAt": exp_ms}})
    )
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", creds)
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "absent.json")
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    s = discovery.oauth_status("anthropic-oauth")

    assert s.durability == discovery.DURABILITY_BORROWED
    assert s.refreshable is False
    assert s.expires_at == pytest.approx(exp_ms / 1000.0)


def test_status_calls_an_env_token_static_with_no_expiry(monkeypatch):
    """The one path protoAgent never refreshes and cannot inspect. `None` must mean
    UNKNOWN — reporting it as fine is how it reads green until it 401s."""
    from graph.providers import discovery

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-opaque")

    s = discovery.oauth_status("anthropic-oauth")

    assert s.signed_in is True and s.durability == discovery.DURABILITY_STATIC
    assert s.expires_at is None and s.refreshable is False


def test_codex_status_publishes_the_jwt_expiry(monkeypatch, tmp_path):
    from graph.providers import discovery

    exp = time.time() + 900
    store = tmp_path / "codex-oauth.json"
    store.write_text(json.dumps({"tokens": {"access_token": _jwt({"exp": exp}), "refresh_token": "rt"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "none.json")

    s = discovery.oauth_status("openai-codex")

    assert s.expires_at == pytest.approx(exp, abs=1)
    assert s.refreshable is True and s.durability == discovery.DURABILITY_MANAGED


def test_codex_status_survives_an_unreadable_token(monkeypatch, tmp_path):
    """A status poll must never be the thing that breaks — unknown, not an exception."""
    from graph.providers import discovery

    store = tmp_path / "codex-oauth.json"
    store.write_text(json.dumps({"tokens": {"access_token": "not-a-jwt", "refresh_token": "rt"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "none.json")

    assert discovery.oauth_status("openai-codex").expires_at is None


def test_sign_in_hints_lead_with_the_headless_route(monkeypatch, tmp_path):
    """Both hints used to lead with a vendor CLI, and the Claude one offered the env
    var as a co-equal — the one path that never refreshes. Neither mentioned the
    operator-API flow that gives a headless agent an owned, refreshing credential."""
    from graph.providers import discovery

    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "none.json")
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    for provider in ("anthropic-oauth", "openai-codex"):
        hint = discovery.oauth_status(provider).hint
        assert "/api/config/oauth/start" in hint, provider
    claude = discovery.oauth_status("anthropic-oauth").hint
    assert claude.index("/api/config/oauth/start") < claude.index("CLAUDE_CODE_OAUTH_TOKEN")
    assert "never refreshed" in claude


def test_list_provider_models_rejects_unknown():
    from graph.providers import discovery

    with pytest.raises(ValueError, match="not a native OAuth provider"):
        discovery.list_provider_models("openai", LangGraphConfig())


def test_anthropic_models_fall_back_without_creds(monkeypatch, tmp_path):
    from graph.providers import discovery

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "none.json")
    models, error = discovery.list_provider_models("anthropic-oauth", LangGraphConfig())
    assert models  # curated fallback, never empty
    assert error  # explains why the live probe was skipped


# ── settings schema: provider is a suggest-dropdown, not a strict enum ───────────


def test_provider_field_is_a_dropdown():
    from graph.settings_schema import FIELDS, build_schema

    field = next(f for f in FIELDS if f.key == "model.provider")
    assert field.type == "select"
    assert field.options_source == "providers"
    # build_schema fills the provider options for the console.
    schema = build_schema(LangGraphConfig())
    entry = next(f for group in schema for f in group["fields"] if f["key"] == "model.provider")
    assert "anthropic-oauth" in entry["options"]
    assert "openai-codex" in entry["options"]


def test_provider_select_still_accepts_custom_value():
    """options_source (dynamic) means validate_flat must NOT reject a custom provider."""
    from graph.settings_schema import validate_flat

    ok, _ = validate_flat({"model.provider": "my-custom-gateway"})
    assert ok


# ── in-console OAuth sign-in flows ──────────────────────────────────────────────


def test_anthropic_login_start_url_is_well_formed():
    from graph.providers import oauth_login as login

    r = login.anthropic_login_start()
    assert r["mode"] == "redirect"
    url = r["authorize_url"]
    assert url.startswith("https://platform.claude.com/oauth/authorize?")
    assert "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" in url
    assert "code_challenge_method=S256" in url
    assert "user%3Ainference" in url  # scope url-encoded


def test_login_flow_store_roundtrip_and_expiry():
    from graph.providers import oauth_login as login

    fid = login._new_flow("anthropic-oauth", {"state": "s", "code_verifier": "v"})
    assert login._get_flow(fid, "anthropic-oauth").data["state"] == "s"
    # wrong provider → treated as expired/invalid
    with pytest.raises(login.OAuthLoginError):
        login._get_flow(fid, "openai-codex")


def test_anthropic_login_complete_exchanges_and_stores(monkeypatch, tmp_path):
    from graph.providers import oauth as oauth_mod2
    from graph.providers import oauth_login as login

    store = tmp_path / "anthropic-oauth.json"
    monkeypatch.setattr(oauth_mod2, "_anthropic_store_path", lambda paths=None: store)

    started = login.anthropic_login_start()
    flow = login._FLOWS[started["flow_id"]]

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "cc-NEW", "refresh_token": "cc-REF", "expires_in": 3600}

    monkeypatch.setattr(login.httpx, "post", lambda url, **kw: _Resp())
    # code arrives as `<code>#<state>`
    result = login.anthropic_login_complete(started["flow_id"], f"the-code#{flow.data['state']}")
    assert result["status"] == "complete"
    assert store.exists()
    saved = json.loads(store.read_text())
    assert saved["access_token"] == "cc-NEW"
    assert saved["refresh_token"] == "cc-REF"
    # and the resolver now sees it
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    creds = oauth_mod2.resolve_anthropic_oauth()
    assert creds.access_token == "cc-NEW"
    assert creds.source == "instance_store"


def test_anthropic_login_complete_rejects_state_mismatch():
    from graph.providers import oauth_login as login

    started = login.anthropic_login_start()
    result = login.anthropic_login_complete(started["flow_id"], "code#WRONGSTATE")
    assert result["status"] == "error"
    assert "state" in result["error"].lower()


def test_codex_login_poll_pending_then_error(monkeypatch):
    from graph.providers import oauth_login as login

    fid = login._new_flow("openai-codex", {"device_auth_id": "d", "user_code": "u"})

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {}

    monkeypatch.setattr(login.httpx, "post", lambda url, **kw: _Resp(404))
    assert login.codex_login_poll(fid)["status"] == "pending"
    monkeypatch.setattr(login.httpx, "post", lambda url, **kw: _Resp(500))
    assert login.codex_login_poll(fid)["status"] == "error"


def test_anthropic_store_refreshes_when_expiring(monkeypatch, tmp_path):
    """A stored token past expiry is refreshed via the refresh_token on resolve."""
    from graph.providers import oauth as oauth_mod2

    store = tmp_path / "anthropic-oauth.json"
    store.write_text(json.dumps({"access_token": "cc-OLD", "refresh_token": "cc-R", "expires_at": 1.0}))
    monkeypatch.setattr(oauth_mod2, "_anthropic_store_path", lambda paths=None: store)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "cc-FRESH", "expires_in": 3600}

    monkeypatch.setattr(oauth_mod2.httpx, "post", lambda url, **kw: _Resp())
    creds = oauth_mod2.resolve_anthropic_oauth()
    assert creds.access_token == "cc-FRESH"
    # refresh_token preserved (response omitted it)
    assert json.loads(store.read_text())["refresh_token"] == "cc-R"


# ── Credential lifecycle: serialized refresh (#2441) + disconnect (#2440) ────────


def _codex_store(tmp_path, tokens: dict, provenance: str | None = None) -> "types.SimpleNamespace":
    """A SimpleNamespace paths object + a codex store seeded with `tokens`.
    ``provenance`` (#2461): "device_login" = protoAgent-minted (revocable),
    "cli_bootstrap" = borrowed from the CLI, None = a legacy pre-provenance store."""
    doc = {"tokens": tokens}
    if provenance:
        doc["provenance"] = provenance
    (tmp_path / "codex-oauth.json").write_text(json.dumps(doc))
    return types.SimpleNamespace(config_dir=tmp_path)


def _patch_codex(monkeypatch, paths, cli_file):
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda p=None: paths.config_dir / "codex-oauth.json")
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli_file)
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: paths)


def test_codex_concurrent_refresh_spends_the_token_once(monkeypatch, tmp_path):
    """#2441: two simultaneous resolutions must make ONE refresh request and both return
    the rotated token — never race the single-use refresh token to a 400."""
    paths = _codex_store(
        tmp_path, {"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "single-use", "account_id": "a"}
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")
    fresh = _jwt({"exp": time.time() + 3600})
    calls = {"n": 0}
    guard = threading.Lock()

    def fake_post(url, **kw):
        with guard:
            calls["n"] += 1
            first = calls["n"] == 1
        time.sleep(0.05)  # widen the window a real race would exploit
        return types.SimpleNamespace(
            status_code=200 if first else 400,
            json=lambda: {"access_token": fresh, "refresh_token": "rot"} if first else {"error": "invalid_grant"},
        )

    monkeypatch.setattr(oauth_mod.httpx, "post", fake_post)
    out: dict[int, str] = {}
    threads = [
        threading.Thread(target=lambda i=i: out.__setitem__(i, resolve_codex_oauth(paths).access_token))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1  # the refresh happened once
    assert out == {0: fresh, 1: fresh}  # both callers got the rotated token


def test_codex_warm_read_neither_refreshes_nor_writes(monkeypatch, tmp_path):
    """A warm, unexpired store read is lock-free and touches no network/disk (#2441)."""
    paths = _codex_store(
        tmp_path, {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")

    def boom(*a, **k):
        raise AssertionError("warm read must not hit the network")

    monkeypatch.setattr(oauth_mod.httpx, "post", boom)
    before = (paths.config_dir / "codex-oauth.json").stat().st_mtime_ns
    creds = resolve_codex_oauth(paths)
    assert creds.source == "instance_store"
    assert (paths.config_dir / "codex-oauth.json").stat().st_mtime_ns == before  # no rewrite


def test_cancel_login_drops_the_pending_flow(monkeypatch):
    from graph.providers import oauth_login as login

    started = login.anthropic_login_start()
    assert login.cancel_login(started["flow_id"]) == {"ok": True, "cancelled": True}
    # the flow is gone — completing it now fails as an expired session (the route maps
    # this OAuthLoginError to an error response)
    with pytest.raises(login.OAuthLoginError):
        login.anthropic_login_complete(started["flow_id"], "code#state")
    # cancelling an unknown flow is a no-op, not an error
    assert login.cancel_login("nope")["cancelled"] is False


def test_disconnect_codex_revokes_removes_and_leaves_cli_untouched(monkeypatch, tmp_path):
    """#2440: revoke best-effort, delete OUR store, never touch ~/.codex/auth.json.
    The store is protoAgent-minted (#2461) — the case where remote revoke is correct."""
    paths = _codex_store(
        tmp_path,
        {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"},
        provenance="device_login",
    )
    cli = tmp_path / "codex_cli.json"
    cli_body = json.dumps({"tokens": {"access_token": "cli", "refresh_token": "clir"}})
    cli.write_text(cli_body)
    _patch_codex(monkeypatch, paths, cli)
    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: types.SimpleNamespace(status_code=200))

    result = oauth_mod.disconnect("openai-codex", paths)
    assert result.removed and result.revoked
    assert not (paths.config_dir / "codex-oauth.json").exists()  # our copy gone
    assert cli.read_text() == cli_body  # the Codex CLI's own file is byte-identical


def test_disconnect_removes_local_even_when_revoke_fails(monkeypatch, tmp_path):
    paths = _codex_store(
        tmp_path,
        {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"},
        provenance="device_login",
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")

    def failing_post(url, **kw):
        raise oauth_mod.httpx.HTTPError("network down")

    monkeypatch.setattr(oauth_mod.httpx, "post", failing_post)
    result = oauth_mod.disconnect("openai-codex", paths)
    assert result.removed is True and result.revoked is False  # local removal still guaranteed
    assert not (paths.config_dir / "codex-oauth.json").exists()


def test_disconnect_is_idempotent(monkeypatch, tmp_path):
    paths = _codex_store(
        tmp_path, {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")
    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: types.SimpleNamespace(status_code=200))
    first = oauth_mod.disconnect("openai-codex", paths)
    second = oauth_mod.disconnect("openai-codex", paths)
    assert first.removed is True
    assert second.removed is False  # nothing left to remove; still succeeds


def test_disconnect_suppresses_cli_reimport_until_reconnect(monkeypatch, tmp_path):
    """#2440 core: after an explicit disconnect, resolve must NOT silently re-bootstrap
    from the Codex CLI until an in-console sign-in reconnects."""
    paths = types.SimpleNamespace(config_dir=tmp_path)
    cli = tmp_path / "codex_cli.json"
    cli.write_text(
        json.dumps(
            {"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}}
        )
    )
    _patch_codex(monkeypatch, paths, cli)

    assert resolve_codex_oauth(paths).source == "codex_cli_bootstrap"  # imports once
    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: types.SimpleNamespace(status_code=200))
    oauth_mod.disconnect("openai-codex", paths)

    with pytest.raises(OAuthCredentialError, match="disconnected"):
        resolve_codex_oauth(paths)  # would previously re-import from the CLI — now suppressed

    oauth_mod.clear_disconnected("openai-codex", paths)  # an explicit sign-in clears the intent
    assert resolve_codex_oauth(paths).source == "codex_cli_bootstrap"  # reconnect works


def test_disconnect_marker_is_owner_only_on_posix(monkeypatch, tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX mode bits; Windows uses the icacls ACL contract (atomic_write funnel)")
    paths = _codex_store(
        tmp_path, {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")
    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: types.SimpleNamespace(status_code=200))
    oauth_mod.disconnect("openai-codex", paths)
    marker = tmp_path / "oauth-disconnected.json"
    assert marker.exists()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600  # credential-adjacent state is owner-only


def test_disconnect_anthropic_removes_store_and_suppresses(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    paths = types.SimpleNamespace(config_dir=tmp_path)
    (tmp_path / "anthropic-oauth.json").write_text(
        json.dumps({"access_token": "cc-X", "refresh_token": "cc-R", "expires_at": time.time() + 3600})
    )
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda p=None: tmp_path / "anthropic-oauth.json")
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: paths)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "no-claude.json")

    result = oauth_mod.disconnect("anthropic-oauth", paths)
    assert result.removed is True and result.revoked is False
    assert not (tmp_path / "anthropic-oauth.json").exists()
    with pytest.raises(OAuthCredentialError, match="disconnected"):
        resolve_anthropic_oauth()  # suppressed until reconnect


def test_disconnect_never_revokes_a_bootstrap_borrowed_credential(monkeypatch, tmp_path):
    """#2461: a CLI-bootstrap token set is the Codex CLI's login, borrowed —
    disconnect deletes protoAgent's copy and must NOT hit the revoke endpoint."""
    paths = _codex_store(
        tmp_path,
        {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"},
        provenance="cli_bootstrap",
    )
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")

    def _no_network(url, **kw):
        raise AssertionError(f"remote revoke attempted for a borrowed credential: {url}")

    monkeypatch.setattr(oauth_mod.httpx, "post", _no_network)
    result = oauth_mod.disconnect("openai-codex", paths)
    assert result.removed is True and result.revoked is False
    assert "borrowed" in result.note
    assert not (paths.config_dir / "codex-oauth.json").exists()


def test_disconnect_treats_legacy_no_provenance_store_as_borrowed(monkeypatch, tmp_path):
    """A store written before provenance existed proves nothing about ownership —
    the safe scope is local-delete-only."""
    paths = _codex_store(tmp_path, {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r"})
    _patch_codex(monkeypatch, paths, tmp_path / "no-cli.json")
    monkeypatch.setattr(
        oauth_mod.httpx, "post", lambda url, **kw: (_ for _ in ()).throw(AssertionError("revoke attempted"))
    )
    result = oauth_mod.disconnect("openai-codex", paths)
    assert result.removed is True and result.revoked is False


def test_bootstrap_stamps_cli_provenance_and_refresh_preserves_it(monkeypatch, tmp_path):
    """Resolution's bootstrap write records cli_bootstrap; a later refresh
    rewrite keeps it (a refresh rotates tokens, not ownership)."""
    cli = tmp_path / "codex_auth.json"
    fresh = _jwt({"exp": time.time() + 3600})
    cli.write_text(json.dumps({"tokens": {"access_token": fresh, "refresh_token": "r", "account_id": "acct-1"}}))
    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)

    resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert json.loads(store.read_text())["provenance"] == "cli_bootstrap"

    # Force a refresh rewrite; provenance must survive.
    stale = _jwt({"exp": time.time() - 10})
    doc = json.loads(store.read_text())
    doc["tokens"]["access_token"] = stale
    store.write_text(json.dumps(doc))

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r2"}

    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: _Resp())
    resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert json.loads(store.read_text())["provenance"] == "cli_bootstrap"


def test_device_login_stamps_owned_provenance(monkeypatch, tmp_path):
    """The in-console device sign-in mints the login itself — its store write
    records device_login, the one provenance disconnect may revoke."""
    from graph.providers import oauth_login as login

    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: types.SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(
        login,
        "_exchange_codex_code",
        lambda code, verifier: {"access_token": "at", "refresh_token": "rt", "id_token": ""},
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"authorization_code": "ac", "code_verifier": "cv"}

    monkeypatch.setattr(login.httpx, "post", lambda url, **kw: _Resp())
    flow_id = login._new_flow("openai-codex", {"device_auth_id": "d", "user_code": "u"})
    assert login.codex_login_poll(flow_id) == {"status": "complete"}
    assert json.loads(store.read_text())["provenance"] == "device_login"


def test_anthropic_oauth_reads_macos_keychain(monkeypatch, tmp_path):
    """macOS: Claude Code stores its login in the Keychain, not
    ~/.claude/.credentials.json — the file-only read left the borrow-the-CLI-login
    story dead on the primary desktop platform (live provider switch failed with
    Claude Code signed in right there)."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "no-store.json")
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: types.SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_keychain",
        lambda: {"claudeAiOauth": {"accessToken": "sk-ant-oat-kc", "expiresAt": (time.time() + 3600) * 1000}},
    )
    creds = oauth_mod.resolve_anthropic_oauth()
    assert creds.access_token == "sk-ant-oat-kc"
    assert creds.source == "keychain"


def test_keychain_reader_parses_security_output(monkeypatch):
    """The helper itself: darwin-gated, tolerant of absence/garbage, parses the
    security(1) -w JSON payload. (The autouse fixture stubs the module attr, so
    exercise the real function through a restore.)"""
    monkeypatch.setattr(oauth_mod, "_read_claude_keychain", _REAL_KEYCHAIN_READ)
    monkeypatch.setattr(oauth_mod.sys, "platform", "darwin")

    def _fake_run(cmd, **kw):
        assert cmd[:2] == ["security", "find-generic-password"]
        return types.SimpleNamespace(returncode=0, stdout='{"claudeAiOauth": {"accessToken": "t"}}')

    monkeypatch.setattr(oauth_mod.subprocess, "run", _fake_run)
    assert oauth_mod._read_claude_keychain() == {"claudeAiOauth": {"accessToken": "t"}}

    monkeypatch.setattr(
        oauth_mod.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=44, stdout=""),
    )
    assert oauth_mod._read_claude_keychain() is None  # absent item

    monkeypatch.setattr(
        oauth_mod.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="not json"),
    )
    assert oauth_mod._read_claude_keychain() is None  # garbage payload

    monkeypatch.setattr(oauth_mod.sys, "platform", "win32")
    assert oauth_mod._read_claude_keychain() is None  # never shells out off-macOS


def test_disconnect_suppresses_keychain_too(monkeypatch, tmp_path):
    """#2440's contract extends to the new source: after an explicit disconnect,
    the Keychain login must NOT silently re-resolve."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "no-store.json")
    paths = types.SimpleNamespace(config_dir=tmp_path)
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: paths)
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_keychain",
        lambda: {"claudeAiOauth": {"accessToken": "sk-ant-oat-kc"}},
    )
    oauth_mod._mark_disconnected(paths, "anthropic-oauth")
    with pytest.raises(oauth_mod.OAuthCredentialError):
        oauth_mod.resolve_anthropic_oauth()


# ── #2582: the token must not be frozen at graph-build time ─────────────────


@pytest.fixture(autouse=True)
def _clear_oauth_token_cache():
    from graph.providers import anthropic_oauth as ao

    ao._reset_token_cache()
    yield
    ao._reset_token_cache()


def test_rotated_token_reaches_the_live_client(monkeypatch):
    """The bug: the token was resolved ONCE at graph build and frozen into the client for
    the process lifetime. This agent rotates the shared Claude credential by doing its job
    — its own Claude Code coders refresh it, invalidating the previous access token — so
    every call 401'd as "revoked" while oauth-status kept reporting a healthy sign-in."""
    from graph.providers import anthropic_oauth as ao

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-OLD")
    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-sonnet-4-5"))
    assert llm._client.auth_token == "cc-OLD"

    # A coder run refreshes the shared credential out from under us.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-NEW")
    ao._reset_token_cache()  # the TTL would otherwise hold the old value briefly
    llm._refresh_oauth_token()

    assert llm._client.auth_token == "cc-NEW"
    assert llm._async_client.auth_token == "cc-NEW"  # both clients, or async turns still 401
    assert llm.oauth_token == "cc-NEW"


def test_every_request_entry_point_refreshes_first(monkeypatch):
    """All four of _generate/_stream/_agenerate/_astream must refresh — a turn that only
    ever streams would otherwise keep presenting the dead token."""
    from graph.providers import anthropic_oauth as ao

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-OLD")
    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-sonnet-4-5"))

    for name in ("_generate", "_stream", "_agenerate", "_astream"):
        assert name in type(llm).__dict__, f"{name} must be overridden to refresh the token"

    calls: list[str] = []
    monkeypatch.setattr(type(llm), "_refresh_oauth_token", lambda self: calls.append("refreshed"))
    monkeypatch.setattr(ao.ChatAnthropic, "_generate", lambda self, *a, **k: "ok")
    assert llm._generate([]) == "ok"
    assert calls == ["refreshed"]


def test_token_resolution_is_ttl_cached(monkeypatch):
    """Resolution can shell out to the macOS Keychain, so a per-call resolve would add a
    subprocess spawn to every model call. The TTL keeps that off the hot path."""
    from graph.providers import anthropic_oauth as ao

    resolves = {"n": 0}

    def _counted():
        resolves["n"] += 1
        return types.SimpleNamespace(access_token=f"tok-{resolves['n']}")

    monkeypatch.setattr(ao, "resolve_anthropic_oauth", _counted)

    assert ao.current_oauth_token() == "tok-1"
    assert ao.current_oauth_token() == "tok-1"  # served from cache
    assert resolves["n"] == 1
    assert ao.current_oauth_token(force=True) == "tok-2"  # force bypasses it
    assert resolves["n"] == 2


def test_a_broken_store_read_keeps_the_working_token(monkeypatch):
    """This runs before every request, so a transient keychain hiccup must not take down a
    live turn — the existing token is still the best guess available."""
    from graph.providers import anthropic_oauth as ao

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-GOOD")
    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-sonnet-4-5"))

    def _boom(**kwargs):
        raise OSError("keychain unavailable")

    monkeypatch.setattr(ao, "current_oauth_token", _boom)
    llm._refresh_oauth_token()  # must not raise

    assert llm._client.auth_token == "cc-GOOD"
