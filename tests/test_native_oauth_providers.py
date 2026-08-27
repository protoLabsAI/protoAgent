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


def test_codex_never_silently_imports_the_cli_login(monkeypatch, tmp_path):
    """Resolution does NOT read the Codex CLI's credential.

    A refresh token is single-use, so a silent import puts two applications on one
    secret with no way to coordinate — we cannot lock the Codex CLI, and whichever
    refreshes first kills the other. Ownership is explicit instead.
    """
    cli = tmp_path / "codex_auth.json"
    cli.write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}})
    )
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)

    def _never(*a, **kw):
        raise AssertionError("resolution must not spend the CLI's token")

    monkeypatch.setattr(oauth_mod.httpx, "post", _never)
    with pytest.raises(OAuthCredentialError, match="Not signed in"):
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert not store.exists()


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


# ── burned-refresh-token recovery ───────────────────────────────────────────────
#
# Every instance that bootstraps from the same ~/.codex/auth.json copies the SAME
# single-use refresh token, so the first one to refresh burns it for the others. That
# 401 used to be permanent: we only bootstrap when the store file is missing, so a
# store holding a dead token never re-read the CLI file — and the error's own advice
# ("run codex login") could not work. These pin the recovery and its four guards.


class _CodexRefreshStub:
    """Stands in for ``httpx.post``: mints for known tokens, 401s for the rest."""

    def __init__(self, mint: dict[str, str]) -> None:
        self.mint = mint  # refresh_token sent -> access token handed back
        self.sent: list[str] = []

    def __call__(self, url, **kw):
        sent = kw.get("data", {}).get("refresh_token")
        self.sent.append(sent)
        access = self.mint.get(sent)
        if access is None:
            return types.SimpleNamespace(status_code=401, json=lambda: {})
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": access, "refresh_token": f"{sent}-rotated"},
        )


def _burned_store_and_cli(tmp_path, monkeypatch, *, cli_tokens):
    """A store whose access token is stale and whose refresh token is already spent."""
    store = tmp_path / "codex-oauth.json"
    store.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt({"exp": time.time() - 10}),
                    "refresh_token": "r-burned",
                    "account_id": "acct-ours",
                }
            }
        )
    )
    cli = tmp_path / "codex_auth.json"
    cli.write_text(json.dumps({"tokens": cli_tokens}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    return store


def test_codex_rejected_refresh_reimports_newer_cli_credential(monkeypatch, tmp_path):
    """A sibling instance burned our token; the CLI's fresh login rescues us."""
    fresh = _jwt({"exp": time.time() + 3600})
    store = _burned_store_and_cli(
        tmp_path,
        monkeypatch,
        cli_tokens={"access_token": fresh, "refresh_token": "r-cli-new", "account_id": "acct-cli"},
    )
    stub = _CodexRefreshStub({})  # every refresh 401s
    monkeypatch.setattr(oauth_mod.httpx, "post", stub)

    creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))

    assert creds.access_token == fresh
    assert creds.account_id == "acct-cli"
    assert stub.sent == ["r-burned"]  # CLI token was still valid — not spent needlessly
    saved = json.loads(store.read_text())
    assert saved["tokens"]["refresh_token"] == "r-cli-new"
    # Re-imported, so it is borrowed again: disconnect must not revoke it remotely (#2461).
    assert saved["provenance"] == oauth_mod.PROVENANCE_CLI_BOOTSTRAP


def test_codex_no_reimport_of_a_stale_cli_login(monkeypatch, tmp_path):
    """A CLI login whose own access token has expired is NOT adopted.

    Its refresh token is single-use and by then almost certainly spent, so importing it
    trades an honest "sign in" for a 401 about a refresh — the failure Hermes calls
    getting "stuck with 'Login successful!' but no working credentials".
    """
    _burned_store_and_cli(
        tmp_path,
        monkeypatch,
        cli_tokens={"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "r-cli-stale"},
    )
    stub = _CodexRefreshStub({"r-cli-stale": _jwt({"exp": time.time() + 3600})})
    monkeypatch.setattr(oauth_mod.httpx, "post", stub)

    with pytest.raises(OAuthCredentialError):
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))

    # The stale CLI token is never spent — only our own burned one was tried.
    assert stub.sent == ["r-burned"]


def test_codex_bootstrap_refuses_a_stale_cli_login(monkeypatch, tmp_path):
    """With no store of our own, a stale CLI login is a sign-in problem, not a refresh one."""
    cli = tmp_path / "codex_auth.json"
    cli.write_text(json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "r-stale"}}))
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "store-nope.json")

    def _never(*a, **kw):
        raise AssertionError("a stale CLI credential must not be spent on a refresh")

    monkeypatch.setattr(oauth_mod.httpx, "post", _never)

    with pytest.raises(OAuthCredentialError) as ei:
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    # The message must name the sign-in, not a refresh failure.
    assert "codex login" in str(ei.value)
    assert "refresh failed" not in str(ei.value).lower()


def test_import_takes_ownership_by_rotating_on_the_way_in(monkeypatch, tmp_path):
    """The explicit handover: import refreshes immediately, so only we hold a live token."""
    cli = tmp_path / "codex_auth.json"
    cli.write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r-cli", "account_id": "acct-1"}})
    )
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    minted = _jwt({"exp": time.time() + 3600})
    stub = _CodexRefreshStub({"r-cli": minted})
    monkeypatch.setattr(oauth_mod.httpx, "post", stub)

    result = oauth_mod.import_codex_cli_credential(paths=types.SimpleNamespace(config_dir=tmp_path))

    assert result["cli_needs_relogin"] is True
    assert stub.sent == ["r-cli"]  # rotated on the way in — the CLI's copy is now dead
    saved = json.loads(store.read_text())
    assert saved["tokens"]["access_token"] == minted
    assert saved["tokens"]["refresh_token"] == "r-cli-rotated"
    assert saved["provenance"] == oauth_mod.PROVENANCE_CLI_BOOTSTRAP
    # And the CLI's own file is never written.
    assert json.loads(cli.read_text())["tokens"]["refresh_token"] == "r-cli"


def test_import_refuses_a_stale_cli_login(monkeypatch, tmp_path):
    cli = tmp_path / "codex_auth.json"
    cli.write_text(json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "r"}}))
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: tmp_path / "s.json")
    with pytest.raises(OAuthCredentialError, match="no usable login"):
        oauth_mod.import_codex_cli_credential(paths=types.SimpleNamespace(config_dir=tmp_path))


def test_codex_no_reimport_when_cli_holds_the_same_dead_token(monkeypatch, tmp_path):
    """The usual case: the CLI's copy IS the token we just burned. Don't re-spend it."""
    _burned_store_and_cli(
        tmp_path,
        monkeypatch,
        cli_tokens={"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "r-burned"},
    )
    stub = _CodexRefreshStub({})
    monkeypatch.setattr(oauth_mod.httpx, "post", stub)

    with pytest.raises(OAuthCredentialError) as ei:
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))

    assert ei.value.relogin is True
    assert stub.sent == ["r-burned"]  # exactly once — no retry loop, no double burn


def test_codex_no_reimport_while_disconnected(monkeypatch, tmp_path):
    """An explicit disconnect (#2440) outranks repair — never silently re-import."""
    paths = types.SimpleNamespace(config_dir=tmp_path)
    _burned_store_and_cli(
        tmp_path,
        monkeypatch,
        cli_tokens={"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r-cli-new"},
    )
    oauth_mod._write_disconnected(paths, {"openai-codex"})
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({}))

    with pytest.raises(OAuthCredentialError):
        resolve_codex_oauth(paths=paths)


def test_codex_no_reimport_on_network_error(monkeypatch, tmp_path):
    """A blip is not a rejection: an unreachable OpenAI must not rotate us onto the CLI."""
    _burned_store_and_cli(
        tmp_path,
        monkeypatch,
        cli_tokens={"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r-cli-new"},
    )

    def _boom(url, **kw):
        raise oauth_mod.httpx.ConnectError("down")

    monkeypatch.setattr(oauth_mod.httpx, "post", _boom)

    with pytest.raises(OAuthCredentialError) as ei:
        resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))

    assert ei.value.relogin is False


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


def test_identity_string_system_becomes_exact_first_block():
    # Anthropic's OAuth enforcement matches the FIRST BLOCK byte-exactly (#2763) —
    # a merged "{prefix}\n\n{persona}" string is refused with a generic 429, so a
    # string system prompt must be CONVERTED to blocks, never string-prepended.
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    req = _mw()._transform(_FakeReq(SystemMessage(content="You are Aria.")))
    content = req.system_message.content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}  # exact, alone
    assert content[1]["text"] == "You are Aria."


def test_identity_repairs_the_old_merged_string_shape():
    # The old string branch emitted "{prefix}\n\n{persona}" as ONE string — the
    # exact shape Anthropic now refuses. A prompt already in that shape must be
    # SPLIT into the exact-block form, not skipped as already-done (skipping is
    # what kept it failing forever).
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    merged = f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\nYou are Aria."
    req = _mw()._transform(_FakeReq(SystemMessage(content=merged)))
    content = req.system_message.content
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    assert content[1]["text"] == "You are Aria."


def test_identity_is_idempotent():
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    mw = _mw()
    once = mw._transform(_FakeReq(SystemMessage(content="You are Aria.")))
    twice = mw._transform(once)
    content = twice.system_message.content
    assert content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    all_text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    assert all_text.count(CLAUDE_CODE_SYSTEM_PREFIX) == 1


def test_identity_prepends_block_list():
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    blocks = [{"type": "text", "text": "You are Aria.", "cache_control": {"type": "ephemeral"}}]
    req = _mw()._transform(_FakeReq(SystemMessage(content=blocks)))
    content = req.system_message.content
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}  # exact — no extra keys
    assert content[1]["cache_control"] == {"type": "ephemeral"}  # original block untouched


def test_identity_splits_a_first_block_that_starts_with_the_line():
    # A first block that STARTS with the line but carries more text is the other
    # refused shape — the line must be split out into its own exact block, with
    # the original block's other keys (cache_control) kept on the REMAINDER.
    from langchain_core.messages import SystemMessage

    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    blocks = [
        {
            "type": "text",
            "text": f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\nYou are Aria.",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "Volatile context."},
    ]
    req = _mw()._transform(_FakeReq(SystemMessage(content=blocks)))
    content = req.system_message.content
    assert content[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
    assert content[1]["text"] == "You are Aria."
    assert content[1]["cache_control"] == {"type": "ephemeral"}
    assert content[2]["text"] == "Volatile context."


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


def test_status_marks_an_expired_unrefreshable_owned_claude_login_signed_out(monkeypatch, tmp_path):
    from graph.providers import discovery

    store = tmp_path / "anthropic-oauth.json"
    store.write_text(json.dumps({"access_token": "expired", "refresh_token": "", "expires_at": 1.0}))
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: store)

    status = discovery.oauth_status("anthropic-oauth")

    assert status.signed_in is False
    assert status.source == "instance_store"
    assert "expired" in status.detail.lower()
    assert "Sign in again" in status.hint


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


def test_status_refuses_to_call_an_expired_borrowed_claude_login_signed_in(monkeypatch, tmp_path):
    """A Keychain item can outlive its access token. Green "signed in" made model
    discovery silently fall back and every real turn 401 until the operator guessed why."""
    from graph.providers import discovery

    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "absent.json")
    monkeypatch.setattr(oauth_mod, "_read_claude_credentials_file", lambda: None)
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_keychain",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "expired",
                "subscriptionType": "max",
                "expiresAt": (time.time() - 60) * 1000,
            }
        },
    )

    status = discovery.oauth_status("anthropic-oauth")

    assert status.signed_in is False
    assert status.source == "keychain"
    assert status.durability == discovery.DURABILITY_BORROWED
    assert "expired" in status.detail.lower()
    assert "Sign in" in status.hint


def test_status_does_not_let_an_expired_credentials_file_mask_a_valid_keychain_login(monkeypatch, tmp_path):
    from graph.providers import discovery

    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "absent.json")
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_credentials_file",
        lambda: {"claudeAiOauth": {"accessToken": "expired", "expiresAt": 1}},
    )
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_keychain",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "current",
                "subscriptionType": "max",
                "expiresAt": (time.time() + 3600) * 1000,
            }
        },
    )

    status = discovery.oauth_status("anthropic-oauth")

    assert status.signed_in is True
    assert status.source == "keychain"
    assert status.detail == "max plan"


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


def test_anthropic_models_do_not_invent_a_catalog_without_creds(monkeypatch, tmp_path):
    from graph.providers import discovery

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "none.json")
    models, error = discovery.list_provider_models("anthropic-oauth", LangGraphConfig())
    assert models == []
    assert error  # explains why the live probe was skipped


def test_anthropic_models_are_the_live_api_response(monkeypatch):
    from graph.providers import discovery

    monkeypatch.setattr(
        oauth_mod,
        "resolve_anthropic_oauth",
        lambda: oauth_mod.AnthropicOAuthCreds("live-token", "instance_store"),
    )

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "claude-live-a"}, {"id": "claude-live-b"}]}

    monkeypatch.setattr(discovery.httpx, "get", lambda *args, **kwargs: _Resp())

    models, error = discovery.list_provider_models("anthropic-oauth", LangGraphConfig())

    assert models == ["claude-live-a", "claude-live-b"]
    assert error == ""


def test_anthropic_model_probe_failure_is_not_replaced_with_static_ids(monkeypatch):
    from graph.providers import discovery

    monkeypatch.setattr(
        oauth_mod,
        "resolve_anthropic_oauth",
        lambda: oauth_mod.AnthropicOAuthCreds("expired-token", "keychain"),
    )

    def _failed_probe(*args, **kwargs):
        raise discovery.httpx.HTTPError("401 token expired")

    monkeypatch.setattr(discovery.httpx, "get", _failed_probe)

    models, error = discovery.list_provider_models("anthropic-oauth", LangGraphConfig())

    assert models == []
    assert "401 token expired" in error
    assert "exact model ID" in error


class _CaptureStreamLLM:
    """Records the messages handed to `.stream()` and yields one chunk."""

    def __init__(self):
        self.captured = None

    def stream(self, messages):
        self.captured = list(messages)
        yield "ok"


def test_validate_oauth_sends_identity_prefix_for_anthropic(monkeypatch):
    """Anthropic's OAuth infra refuses traffic whose system prompt doesn't lead with
    the Claude Code identity line (429). Real turns get it from
    ClaudeCodeIdentityMiddleware; the probe bypasses the middleware stack, so it must
    carry the prefix itself — a bare HumanMessage made "Test connection" fail while
    real turns worked."""
    from langchain_core.messages import SystemMessage

    from graph.providers import discovery
    from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX

    llm = _CaptureStreamLLM()
    monkeypatch.setattr("graph.providers.build_native_oauth_llm", lambda *a, **k: llm)
    ok, error = discovery.validate_oauth_connection("anthropic-oauth", "claude-sonnet-4-5", LangGraphConfig())
    assert ok is True and error == ""
    first = llm.captured[0]
    assert isinstance(first, SystemMessage)
    assert first.content == CLAUDE_CODE_SYSTEM_PREFIX


def test_validate_oauth_no_system_message_for_codex(monkeypatch):
    """The Codex Responses backend rejects system-role items — the probe must stay a
    bare user prompt for openai-codex."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from graph.providers import discovery

    llm = _CaptureStreamLLM()
    monkeypatch.setattr("graph.providers.build_native_oauth_llm", lambda *a, **k: llm)
    ok, error = discovery.validate_oauth_connection("openai-codex", "gpt-5-codex", LangGraphConfig())
    assert ok is True and error == ""
    assert not any(isinstance(m, SystemMessage) for m in llm.captured)
    assert isinstance(llm.captured[0], HumanMessage)


# ── settings schema: provider is a suggest-dropdown, not a strict enum ───────────


def test_provider_field_is_retired_from_the_form_but_still_a_known_key():
    """ADR 0106: Connections owns the endpoint/key/provider triple.

    The field stays in FIELDS so it round-trips through config_to_dict, the YAML writer
    and /api/settings — forks, snapshot import and the fleet Host layer read it — but a
    form that still offered it would contradict the panel above it. Removed no earlier
    than v0.152.0.
    """
    from graph.settings_schema import FIELDS, build_schema

    field = next(f for f in FIELDS if f.key == "model.provider")
    assert field.ui_hidden is True
    assert field.options_source == "providers"  # still validates a legacy written value

    rendered = {f["key"] for group in build_schema(LangGraphConfig()) for f in group["fields"]}
    assert "model.provider" not in rendered
    assert "model.api_base" not in rendered
    assert "model.api_key" not in rendered

    # ...but the cascade diagnostic can still explain where each one came from.
    explained = {f["key"] for g in build_schema(LangGraphConfig(), include_hidden=True) for f in g["fields"]}
    assert {"model.provider", "model.api_base", "model.api_key"} <= explained


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


def test_anthropic_store_refuses_an_expired_token_without_refresh_token(monkeypatch, tmp_path):
    from graph.providers import oauth as oauth_mod2

    store = tmp_path / "anthropic-oauth.json"
    store.write_text(json.dumps({"access_token": "cc-OLD", "refresh_token": "", "expires_at": 1.0}))
    monkeypatch.setattr(oauth_mod2, "_anthropic_store_path", lambda paths=None: store)

    with pytest.raises(OAuthCredentialError, match="has expired and cannot be refreshed"):
        oauth_mod2.resolve_anthropic_oauth()


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


def test_disconnect_suppresses_reimport_until_reconnect(monkeypatch, tmp_path):
    """#2440 core, now on the explicit path: a disconnected provider refuses import."""
    paths = types.SimpleNamespace(config_dir=tmp_path)
    cli = tmp_path / "codex_cli.json"
    cli.write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "a"}})
    )
    _patch_codex(monkeypatch, paths, cli)
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r": _jwt({"exp": time.time() + 3600})}))

    oauth_mod.import_codex_cli_credential(paths)
    assert resolve_codex_oauth(paths).source == "instance_store"

    monkeypatch.setattr(oauth_mod.httpx, "post", lambda url, **kw: types.SimpleNamespace(status_code=200))
    oauth_mod.disconnect("openai-codex", paths)
    with pytest.raises(OAuthCredentialError, match="disconnected"):
        resolve_codex_oauth(paths)

    # An explicit import clears the intent, exactly as an in-console sign-in does.
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r": _jwt({"exp": time.time() + 3600})}))
    oauth_mod.import_codex_cli_credential(paths)
    assert resolve_codex_oauth(paths).source == "instance_store"


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


def test_import_stamps_cli_provenance_and_refresh_preserves_it(monkeypatch, tmp_path):
    """Import records cli_bootstrap; a later refresh keeps it (rotation isn't ownership)."""
    cli = tmp_path / "codex_auth.json"
    cli.write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r", "account_id": "acct-1"}})
    )
    store = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", cli)
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r": _jwt({"exp": time.time() + 3600})}))

    oauth_mod.import_codex_cli_credential(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert json.loads(store.read_text())["provenance"] == "cli_bootstrap"

    doc = json.loads(store.read_text())
    doc["tokens"]["access_token"] = _jwt({"exp": time.time() - 10})
    store.write_text(json.dumps(doc))
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r-rotated": _jwt({"exp": time.time() + 3600})}))
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


def test_anthropic_oauth_rejects_an_expired_borrowed_keychain_token(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod, "_CLAUDE_CREDS_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(oauth_mod, "_anthropic_store_path", lambda paths=None: tmp_path / "no-store.json")
    monkeypatch.setattr(oauth_mod, "instance_paths", lambda: types.SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(
        oauth_mod,
        "_read_claude_keychain",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat-expired",
                "expiresAt": (time.time() - 60) * 1000,
            }
        },
    )

    with pytest.raises(OAuthCredentialError, match="borrowed OAuth token has expired"):
        oauth_mod.resolve_anthropic_oauth()


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


# ── credential scope: box store shared by every instance, instance overrides ────
#
# A ChatGPT/Claude login belongs to the person at the machine, not to one instance.
# Per-instance copies meant a console sign-in on `default` left the desktop app's other
# servers importing (and burning) the vendor CLI's single-use token.


def test_codex_store_defaults_to_the_box_when_the_instance_has_none(monkeypatch, tmp_path):
    box, inst = tmp_path / "box", tmp_path / "inst"
    inst.mkdir()
    monkeypatch.setattr(oauth_mod, "box_root", lambda: box)
    got = oauth_mod._codex_store_path(types.SimpleNamespace(config_dir=inst))
    assert got == box / "codex-oauth.json"


def test_codex_instance_store_overrides_the_box_when_present(monkeypatch, tmp_path):
    box, inst = tmp_path / "box", tmp_path / "inst"
    box.mkdir()
    inst.mkdir()
    (box / "codex-oauth.json").write_text("{}")
    (inst / "codex-oauth.json").write_text("{}")
    monkeypatch.setattr(oauth_mod, "box_root", lambda: box)
    got = oauth_mod._codex_store_path(types.SimpleNamespace(config_dir=inst))
    assert got == inst / "codex-oauth.json"


def test_anthropic_store_follows_the_same_scope_rule(monkeypatch, tmp_path):
    box, inst = tmp_path / "box", tmp_path / "inst"
    inst.mkdir()
    monkeypatch.setattr(oauth_mod, "box_root", lambda: box)
    paths = types.SimpleNamespace(config_dir=inst)
    assert oauth_mod._anthropic_store_path(paths) == box / "anthropic-oauth.json"
    (inst / "anthropic-oauth.json").write_text("{}")
    assert oauth_mod._anthropic_store_path(paths) == inst / "anthropic-oauth.json"


def test_legacy_oauth_promotion_rolls_back_if_second_box_write_fails(monkeypatch, tmp_path):
    """The two-provider compatibility transfer cannot strand only one credential at
    box scope when the second durable write fails."""
    box, inst = tmp_path / "box", tmp_path / "inst"
    inst.mkdir()
    codex = inst / "codex-oauth.json"
    anthropic = inst / "anthropic-oauth.json"
    codex.write_text('{"tokens":{"refresh_token":"codex"}}')
    anthropic.write_text('{"refresh_token":"claude"}')
    monkeypatch.setattr(oauth_mod, "box_root", lambda: box)
    real_atomic_write = oauth_mod.atomic_write

    def fail_anthropic(path, text, **kwargs):
        if path == box / "anthropic-oauth.json":
            raise OSError("disk full")
        return real_atomic_write(path, text, **kwargs)

    monkeypatch.setattr(oauth_mod, "atomic_write", fail_anthropic)
    with pytest.raises(oauth_mod.OAuthStoreTransferError, match="could not transfer"):
        oauth_mod.promote_instance_oauth_to_box(inst)

    assert codex.exists() and anthropic.exists()
    assert not (box / "codex-oauth.json").exists()
    assert not (box / "anthropic-oauth.json").exists()


def test_one_box_signin_serves_every_instance(monkeypatch, tmp_path):
    """The whole point: sign in once, and an instance that never signed in resolves."""
    box = tmp_path / "box"
    box.mkdir()
    fresh = _jwt({"exp": time.time() + 3600})
    (box / "codex-oauth.json").write_text(
        json.dumps({"tokens": {"access_token": fresh, "refresh_token": "r-box", "account_id": "acct-box"}})
    )
    monkeypatch.setattr(oauth_mod, "box_root", lambda: box)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "no-cli.json")

    for name in ("default", "dev", "desktop-1"):
        cfg = tmp_path / name
        cfg.mkdir()
        creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=cfg))
        assert creds.access_token == fresh
        assert creds.account_id == "acct-box"
        # Nothing was written into the instance — it reads the shared store in place.
        assert not (cfg / "codex-oauth.json").exists()


def test_codex_adopts_a_token_a_peer_process_just_rotated(monkeypatch, tmp_path):
    """Two processes share the box store; the loser's 401 is not a dead login."""
    store = tmp_path / "codex-oauth.json"
    stale = _jwt({"exp": time.time() - 10})
    store.write_text(json.dumps({"tokens": {"access_token": stale, "refresh_token": "r-mine", "account_id": "a"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "no-cli.json")

    winner = _jwt({"exp": time.time() + 3600})

    def _post(url, **kw):
        # Our spend is refused — a peer got there first and left its result on disk.
        store.write_text(
            json.dumps({"tokens": {"access_token": winner, "refresh_token": "r-peer", "account_id": "a"}})
        )
        return types.SimpleNamespace(status_code=401, json=lambda: {})

    monkeypatch.setattr(oauth_mod.httpx, "post", _post)
    creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert creds.access_token == winner


def test_file_lock_is_best_effort_and_never_raises(tmp_path):
    """An unlockable filesystem degrades to the old behaviour, it does not fail a turn."""
    with oauth_mod._file_lock(tmp_path / "nope" / "deep" / "store.json"):
        pass  # must not raise even though the directory chain is created on demand


# ── earliest_refresh_at: refresh when the PROVIDER says, not on our own skew ────
#
# Access tokens last ~10 days and OpenAI returns `earliest_refresh_at` about a day
# before expiry. Honouring it turns refresh from a per-boot expiry check into a
# roughly once-per-nine-days event, which is what makes a race over a single-use
# token rare rather than routine.


def test_a_live_token_before_the_earliest_mark_is_not_refreshed():
    tokens = {"access_token": _jwt({"exp": time.time() + 86400}), "earliest_refresh_at": time.time() + 3600}
    assert oauth_mod._codex_needs_refresh(tokens, 120) is False


def test_a_live_token_past_the_earliest_mark_is_refreshed():
    tokens = {"access_token": _jwt({"exp": time.time() + 86400}), "earliest_refresh_at": time.time() - 1}
    assert oauth_mod._codex_needs_refresh(tokens, 120) is True


def test_expiry_overrides_a_stale_earliest_mark():
    """A hint that outlives its token must never strand a caller."""
    tokens = {"access_token": _jwt({"exp": time.time() + 10}), "earliest_refresh_at": time.time() + 999_999}
    assert oauth_mod._codex_needs_refresh(tokens, 120) is True


def test_without_the_hint_the_decision_is_expiry_only():
    assert oauth_mod._codex_needs_refresh({"access_token": _jwt({"exp": time.time() + 86400})}, 120) is False
    assert oauth_mod._codex_needs_refresh({"access_token": _jwt({"exp": time.time() - 1})}, 120) is True
    assert oauth_mod._codex_needs_refresh({"access_token": ""}, 120) is True


def test_a_non_numeric_hint_is_ignored_rather_than_trusted():
    tokens = {"access_token": _jwt({"exp": time.time() + 86400}), "earliest_refresh_at": "soon"}
    assert oauth_mod._codex_needs_refresh(tokens, 120) is False


def test_the_hint_is_persisted_by_a_refresh(monkeypatch, tmp_path):
    """Every process on the box must see the same answer, so it rides the store."""
    store = tmp_path / "codex-oauth.json"
    store.write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() - 10}), "refresh_token": "r", "account_id": "a"}})
    )
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    mark = time.time() + 777_777

    def _post(url, **kw):
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": _jwt({"exp": time.time() + 86400}),
                "refresh_token": "r2",
                "earliest_refresh_at": mark,
            },
        )

    monkeypatch.setattr(oauth_mod.httpx, "post", _post)
    resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert json.loads(store.read_text())["tokens"]["earliest_refresh_at"] == mark

    # And the next resolution honours it instead of refreshing again.
    def _never(*a, **kw):
        raise AssertionError("a token before its earliest_refresh_at must not be refreshed")

    monkeypatch.setattr(oauth_mod.httpx, "post", _never)
    resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))


# ── an access token can die before its exp ─────────────────────────────────────
#
# Signing in again on the same ChatGPT account ends the previous session, and the
# backend then answers 401 token_invalidated while the JWT still claims days of
# validity. Deciding "is this good?" from exp alone made that state terminal: nothing
# refreshed, every call failed identically, and the refresh token was live throughout.


def test_marking_a_rejection_clears_the_access_token_but_keeps_the_refresh(monkeypatch, tmp_path):
    store = tmp_path / "codex-oauth.json"
    store.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt({"exp": time.time() + 864000}),  # days of validity left
                    "refresh_token": "r-live",
                    "account_id": "a",
                    "earliest_refresh_at": time.time() + 777_777,
                }
            }
        )
    )
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)

    assert oauth_mod.note_codex_auth_rejected(types.SimpleNamespace(config_dir=tmp_path)) is True
    saved = json.loads(store.read_text())["tokens"]
    assert saved["access_token"] == ""
    assert saved["refresh_token"] == "r-live"  # the live half survives
    # The provider's "don't refresh yet" hint must not pin an invalidated token.
    assert "earliest_refresh_at" not in saved


def test_marking_twice_reports_no_second_change(monkeypatch, tmp_path):
    """So a caller can tell a real invalidation from a repeat and not loop."""
    store = tmp_path / "codex-oauth.json"
    store.write_text(json.dumps({"tokens": {"access_token": _jwt({"exp": time.time() + 3600}), "refresh_token": "r"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    paths = types.SimpleNamespace(config_dir=tmp_path)
    assert oauth_mod.note_codex_auth_rejected(paths) is True
    assert oauth_mod.note_codex_auth_rejected(paths) is False


def test_a_marked_store_refreshes_on_the_next_resolve(monkeypatch, tmp_path):
    """The point of the whole thing: the next call mints a token instead of replaying."""
    store = tmp_path / "codex-oauth.json"
    store.write_text(json.dumps({"tokens": {"access_token": "", "refresh_token": "r-live", "account_id": "a"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "no-cli.json")
    minted = _jwt({"exp": time.time() + 864000})
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r-live": minted}))

    creds = resolve_codex_oauth(paths=types.SimpleNamespace(config_dir=tmp_path))
    assert creds.access_token == minted


def test_force_refresh_overrides_a_token_that_still_looks_valid(monkeypatch, tmp_path):
    store = tmp_path / "codex-oauth.json"
    fine = _jwt({"exp": time.time() + 864000})
    store.write_text(json.dumps({"tokens": {"access_token": fine, "refresh_token": "r-live", "account_id": "a"}}))
    monkeypatch.setattr(oauth_mod, "_codex_store_path", lambda paths: store)
    monkeypatch.setattr(oauth_mod, "_CODEX_CLI_AUTH_FILE", tmp_path / "no-cli.json")
    minted = _jwt({"exp": time.time() + 864000})
    monkeypatch.setattr(oauth_mod.httpx, "post", _CodexRefreshStub({"r-live": minted}))

    paths = types.SimpleNamespace(config_dir=tmp_path)
    assert resolve_codex_oauth(paths=paths).access_token == fine  # unforced: reuses it
    assert resolve_codex_oauth(paths=paths, force_refresh=True).access_token == minted


@pytest.mark.parametrize(
    "text",
    [
        "Error code: 401 - {'error': {'message': 'Your authentication token has been invalidated.', 'code': 'token_invalidated'}}",
        "Error code: 401 - unauthorized",
        "Error code: 401 - {'type': 'invalid_request_error'}",
    ],
)
def test_auth_rejections_are_recognised(text):
    from graph.providers.discovery import _is_auth_rejection

    assert _is_auth_rejection(Exception(text)) is True


@pytest.mark.parametrize("text", ["Connection timed out", "Error code: 500 - server error", "429 rate limited"])
def test_other_failures_never_spend_a_refresh_token(text):
    """Narrow on purpose — the response is to spend a SINGLE-USE token."""
    from graph.providers.discovery import _is_auth_rejection

    assert _is_auth_rejection(Exception(text)) is False
