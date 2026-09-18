"""One resolver for the default route's endpoint and key (#3128 slice).

Every reader that decides "which endpoint and which key does the default gateway route
use" is pinned here across the same matrix of configs, so they cannot drift apart again.
#3525 was one that had: the context-window probe authenticated with `model.api_key` alone
while the client it sized fell back to OPENAI_API_KEY, so env-keyed deployments probed
keyless.

The matrix pins TODAY's semantics, including the one this slice deliberately keeps: the
UNQUALIFIED default route reads the retired `model.api_base` / `model.api_key` (then
OPENAI_API_KEY) and never the registry. A registered connection is used only when a
value names it (`gateway:…`, `local-vllm:…`), and then strictly from its own fields.
Moving the unqualified route onto the registry is the removal slice's decision.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import re
import tokenize
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from graph.config import LangGraphConfig

LEGACY = "https://legacy.example/v1"
REGISTRY = "https://registry.example/v1"
LOCAL = "http://localhost:11434/v1"
VLLM = "http://localhost:8000/v1"
#: The dataclass default a config with no `model.api_base` falls to.
APP_DEFAULT_BASE = LangGraphConfig.api_base

_GATEWAY = {"id": "gateway", "base_url": REGISTRY, "api_key": "registry-key"}

#: id -> (config doc, OPENAI_API_KEY or None, the default route's (endpoint, key)).
CASES = {
    "legacy-only": ({"model": {"api_base": LEGACY, "api_key": "legacy-key"}}, None, (LEGACY, "legacy-key")),
    # What the setup wizard writes (ADR 0106): one connection and a qualified model.name,
    # no legacy fields. The unqualified route does not consult the registry, so it falls
    # to the dataclass default endpoint with no key. Pinned, not endorsed (#3128).
    "registry-only": (
        {"providers": [dict(_GATEWAY)], "model": {"name": "gateway:m"}},
        None,
        (APP_DEFAULT_BASE, ""),
    ),
    # Both set: the unqualified route keeps the legacy pair (and the legacy key beats the
    # env key); a `gateway:` value goes to the registry, pinned in the qualified tests.
    "both": (
        {"providers": [dict(_GATEWAY)], "model": {"api_base": LEGACY, "api_key": "legacy-key"}},
        "env-key",
        (LEGACY, "legacy-key"),
    ),
    "env-key-only": ({"model": {"api_base": LEGACY, "api_key": ""}}, "env-key", (LEGACY, "env-key")),
    "keyless-local": ({"model": {"api_base": LOCAL}}, None, (LOCAL, "")),
    # A provider-qualified lead: the unqualified route is untouched by the connection.
    "qualified-slot": (
        {
            "providers": [{"id": "local-vllm", "base_url": VLLM}],
            "model": {"name": "local-vllm:qwen", "api_base": LEGACY, "api_key": "legacy-key"},
        },
        "env-key",
        (LEGACY, "legacy-key"),
    ),
}


def _load(doc: dict, env: str | None, monkeypatch) -> LangGraphConfig:
    if env is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", env)
    return LangGraphConfig.from_dict(copy.deepcopy(doc))


@pytest.fixture(params=sorted(CASES))
def case(request, monkeypatch):
    doc, env, expected = CASES[request.param]
    return _load(doc, env, monkeypatch), expected


@pytest.fixture
def no_network(monkeypatch):
    """Stub every wire a reader could touch, recording (url, bearer) instead."""
    seen: list[tuple[str, str]] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _record(self, url, headers=None, **_):
            seen.append((str(url), (headers or {}).get("Authorization", "").removeprefix("Bearer ")))

        def get(self, url, headers=None, **kw):
            self._record(url, headers)
            return SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "m"}]})

        def post(self, url, headers=None, **kw):
            # A refusal, so finish_setup stops at its probe and persists nothing.
            self._record(url, headers)
            return SimpleNamespace(status_code=401, json=lambda: {"error": {"message": "no"}}, text="no")

    monkeypatch.setattr("httpx.Client", _Client)
    monkeypatch.setattr("security.egress.check_url", lambda *a, **k: None)
    return seen


# ── the readers, each reduced to the (endpoint, key) it actually uses ─────────────────


def _runtime_client(cfg, monkeypatch, model_name="protolabs/bare"):
    import graph.llm as llm

    monkeypatch.setattr(llm, "_ReasoningChatOpenAI", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("graph.model_window.context_window_for", lambda *a, **k: None)
    built = llm.create_llm(cfg, model_name=model_name)
    return built.base_url, built.api_key


def _embeddings(cfg, monkeypatch):
    import graph.llm as llm

    monkeypatch.setattr(llm, "OpenAIEmbeddings", lambda **kw: SimpleNamespace(**kw))
    cfg.embed_model = "embed-1"
    built = llm._build_embeddings(cfg)
    return built.base_url, built.api_key


def _window_probe(cfg, monkeypatch, model_name="m"):
    from graph import model_window

    seen: list[tuple[str, str]] = []
    model_window.reset_window_cache()
    monkeypatch.setattr(model_window, "_fetch_window_map", lambda base, key: seen.append((base, key)) or {})
    try:
        model_window.context_window_for(cfg, model_name)
    finally:
        model_window.reset_window_cache()
    return seen[0]


def _openshell_policy(cfg) -> str:
    spec = importlib.util.spec_from_file_location(
        "gen_openshell_policy", Path(__file__).parent.parent / "scripts" / "gen_openshell_policy.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_policy(cfg)


def _routes_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.config_routes import register_config_routes

    app = FastAPI()
    register_config_routes(app)
    return TestClient(app)


def _live(cfg, monkeypatch):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)


# ── the matrix ────────────────────────────────────────────────────────────────────────


def test_the_runtime_client_default_route(case, monkeypatch):
    from graph.llm import _build_llm_kwargs

    cfg, expected = case
    kwargs = _build_llm_kwargs(cfg)
    assert (kwargs["base_url"], kwargs["api_key"]) == expected
    assert _runtime_client(cfg, monkeypatch) == expected


def test_knowledge_embeddings(case, monkeypatch):
    cfg, expected = case
    assert _embeddings(cfg, monkeypatch) == expected


def test_the_shared_gateway_http_client(case):
    from graph.llm import _gateway_client_kwargs

    cfg, (base, key) = case
    kwargs = _gateway_client_kwargs(cfg, timeout=1.0)
    assert kwargs["base_url"] == base.rstrip("/")
    assert kwargs["headers"].get("Authorization", "") == (f"Bearer {key}" if key else "")


def test_the_context_window_probe(case, monkeypatch):
    cfg, (base, key) = case
    assert _window_probe(cfg, monkeypatch) == (base.rstrip("/"), key)


def test_is_the_gateway_configured(case):
    from graph.llm import _gateway_configured
    from runtime.acp_runtime import _gateway_configured as acp_gateway_configured

    cfg, (_, key) = case
    assert _gateway_configured(cfg) is bool(key)
    assert acp_gateway_configured(cfg) is bool(key)


def test_headless_validation(case):
    from graph.config_io import validate_for_headless

    cfg, (base, key) = case
    ok, reason = validate_for_headless(cfg)
    assert ok is bool(base and key), reason


def test_a_native_lead_with_no_gateway_key(case):
    from graph.config import _native_provider_without_gateway

    cfg, (_, key) = case
    cfg.model_provider = "anthropic-oauth"
    assert _native_provider_without_gateway(cfg) is (not key)


def test_the_egress_auto_allow_and_the_openshell_policy(case):
    from security import egress

    cfg, (base, _) = case
    host = urlparse(base).hostname
    assert f"- host: {host}  # model / inference gateway" in _openshell_policy(cfg)
    # Drive server.agent_init's own seam, not a hand-rolled copy of it: both boot and
    # live-reload go through `apply_egress_allowlist`, and a call site that stopped
    # auto-allowing the gateway used to leave every test green.
    from server.agent_init import apply_egress_allowlist

    cfg.egress_allowed_hosts = ["example.com"]
    try:
        apply_egress_allowlist(cfg)
        assert host in egress.allowed_hosts()
    finally:
        egress.set_allowed_hosts([])


def test_both_agent_init_egress_call_sites_go_through_the_helper():
    """The seam is only worth having if nothing bypasses it (the boot/reload revert this pins)."""
    code = _code_text((_ROOT / "server" / "agent_init.py").read_text(encoding="utf-8"))
    joined = "\n".join(code)
    assert sum(line.count("egress.set_allowed_hosts (") for line in code) == 1, (
        "server/agent_init.py must call egress.set_allowed_hosts in exactly one place — "
        "apply_egress_allowlist. A second call site can silently drop the gateway auto-allow."
    )
    for site in ("apply_egress_allowlist ( STATE.graph_config )", "apply_egress_allowlist ( new_config )"):
        assert site in joined, f"missing egress helper call: {site}"


def test_the_model_listing_and_test_connection_routes(case, monkeypatch, no_network):
    cfg, (base, key) = case
    _live(cfg, monkeypatch)
    root = base.rstrip("/")
    client = _routes_client()

    client.post("/api/config/models", json={})
    client.post("/api/config/test-model", json={})
    assert no_network[:2] == [(f"{root}/models", key), (f"{root}/chat/completions", key)]


def test_the_settings_schema_model_options(case, monkeypatch, no_network):
    cfg, (base, key) = case
    _live(cfg, monkeypatch)
    _routes_client().get("/api/settings/schema")
    # The first probe is `model.name`'s option list; the lane probes that follow are the
    # registry's, pinned by the provider-registry tests.
    assert no_network[0] == (f"{base.rstrip('/')}/models", key)


def test_the_setup_callbacks_fall_back_to_the_live_route(case, monkeypatch, no_network):
    from server.agent_init import _build_settings_callbacks

    cfg, (base, key) = case
    _live(cfg, monkeypatch)
    callbacks = _build_settings_callbacks()
    root = base.rstrip("/")

    callbacks["list_models"]()
    # A wizard payload naming no connection and no endpoint is probed against the live
    # route. The stubbed refusal stops finish_setup before it persists anything.
    ok, message = callbacks["finish_setup"]({"model": {"name": "protolabs/bare"}}, None)
    assert not ok and "model connection failed" in message
    assert no_network[:2] == [(f"{root}/models", key), (f"{root}/chat/completions", key)]


# ── a qualified value resolves strictly from its own connection ──────────────────────


def test_a_qualified_value_prefers_the_registry_and_never_borrows(monkeypatch):
    both = _load(CASES["both"][0], "env-key", monkeypatch)
    assert _runtime_client(both, monkeypatch, "gateway:m") == (REGISTRY, "registry-key")

    # Migrated connections carry what the legacy route ran on, env key included.
    legacy = _load(CASES["legacy-only"][0], None, monkeypatch)
    assert _runtime_client(legacy, monkeypatch, "gateway:m") == (LEGACY, "legacy-key")
    env_only = _load(CASES["env-key-only"][0], "env-key", monkeypatch)
    assert _runtime_client(env_only, monkeypatch, "gateway:m") == (LEGACY, "env-key")

    # A keyless registered connection gets the placeholder: never the legacy key, never env.
    slot = _load(CASES["qualified-slot"][0], "env-key", monkeypatch)
    assert _runtime_client(slot, monkeypatch, "local-vllm:qwen") == (VLLM, "not-needed")
    assert _runtime_client(slot, monkeypatch, model_name=None) == (VLLM, "not-needed")  # the lead


def test_a_qualified_build_still_sizes_its_window_on_the_default_route(monkeypatch):
    """Pinned, not endorsed: `context_window_for` takes no connection, so a
    `local-vllm:` build asks the DEFAULT route's gateway about the model. Nothing
    crosses credentials (the default route's key goes to its own endpoint), but the
    answer is for the wrong gateway. Left for the removal slice to decide."""
    import graph.llm as llm
    from graph import model_window

    slot = _load(CASES["qualified-slot"][0], "env-key", monkeypatch)
    seen: list[tuple[str, str]] = []
    model_window.reset_window_cache()
    monkeypatch.setattr(model_window, "_fetch_window_map", lambda base, key: seen.append((base, key)) or {})
    monkeypatch.setattr(llm, "_ReasoningChatOpenAI", lambda **kw: SimpleNamespace(**kw))
    try:
        llm.create_llm(slot, model_name="local-vllm:qwen")
    finally:
        model_window.reset_window_cache()
    assert seen == [(LEGACY, "legacy-key")]


# ── a declared connection fails closed (#3128 ruling) ────────────────────────────────


def test_a_declared_connection_probe_never_borrows_the_live_or_env_key(monkeypatch, no_network):
    """`POST /api/config/test-model` naming a REGISTERED connection probes that
    connection's endpoint with that connection's own key — never the live default route's
    key, never OPENAI_API_KEY. `qualified-slot` is the case that can catch a regression:
    `local-vllm` is keyless while a legacy key AND an env key are both present, so
    borrowing anything shows up as `legacy-key`/`env-key` instead of "".
    """
    cfg = _load(CASES["qualified-slot"][0], "env-key", monkeypatch)
    _live(cfg, monkeypatch)
    _routes_client().post("/api/config/test-model", json={"provider": "local-vllm", "model": "qwen"})
    assert no_network[-1] == (f"{VLLM}/chat/completions", "")


def test_a_form_typed_endpoint_probe_never_borrows_the_live_key(monkeypatch, no_network):
    """An endpoint the operator just typed carries its own key or none. Borrowing the saved
    gateway's credential would put it on the wire to an endpoint that never had it."""
    cfg = _load(CASES["both"][0], "env-key", monkeypatch)
    _live(cfg, monkeypatch)
    _routes_client().post("/api/config/test-model", json={"api_base": VLLM, "model": "qwen"})
    assert no_network[-1] == (f"{VLLM}/chat/completions", "")


def test_finish_setup_probes_a_declared_connection_strictly_from_its_own_fields(monkeypatch, no_network):
    """The wizard path: a payload naming a connection probes THAT connection. The stubbed
    refusal stops finish_setup before it persists anything, as in the live-route test above."""
    from server.agent_init import _build_settings_callbacks

    cfg = _load(CASES["both"][0], "env-key", monkeypatch)
    _live(cfg, monkeypatch)
    ok, message = _build_settings_callbacks()["finish_setup"](
        {"providers": [{"id": "local-vllm", "base_url": VLLM}], "model": {"name": "local-vllm:qwen"}}, None
    )
    assert not ok and "model connection failed" in message
    assert no_network[-1] == (f"{VLLM}/chat/completions", "")


def test_a_blank_form_retest_uses_the_live_route_even_when_a_legacy_field_names_a_connection(
    monkeypatch, no_network
):
    """The gap the fail-closed change could have opened: `model.provider` is a RETIRED field,
    so it must not decide which registry entry gets probed. This config's legacy provider
    reads `gateway`, which is also a registered connection id — a blank-form re-test still
    goes to the live route (LEGACY/legacy-key), not the registry entry (REGISTRY/registry-key).
    """
    cfg = _load(
        {
            "providers": [dict(_GATEWAY)],
            "model": {"api_base": LEGACY, "api_key": "legacy-key", "provider": "gateway"},
        },
        "env-key",
        monkeypatch,
    )
    _live(cfg, monkeypatch)
    _routes_client().post("/api/config/test-model", json={"model": "m"})
    assert no_network[-1] == (f"{LEGACY}/chat/completions", "legacy-key")


# ── the resolver itself ───────────────────────────────────────────────────────────────


def test_the_resolver_answers_the_matrix(case):
    from graph.config import resolve_model_route

    cfg, expected = case
    route = resolve_model_route(cfg)
    assert (route.base_url, route.api_key, route.connection) == (*expected, "")


def test_the_resolver_resolves_a_connection_strictly_from_its_own_fields(monkeypatch):
    from graph.config import Provider, resolve_model_route

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    cfg = LangGraphConfig.from_dict({"model": {"api_base": LEGACY, "api_key": "legacy-key"}})
    keyless = resolve_model_route(cfg, Provider(id="local-vllm", base_url=VLLM))
    assert (keyless.base_url, keyless.api_key, keyless.connection) == (VLLM, "", "local-vllm")
    no_base = resolve_model_route(cfg, Provider(id="keyed", api_key=" k "))
    assert (no_base.base_url, no_base.api_key) == ("", "k")


def test_the_resolver_reads_any_config_shaped_object(monkeypatch):
    """Callers hand it SimpleNamespace fakes and partial objects; nothing is required."""
    from graph.config import resolve_model_route

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_model_route(SimpleNamespace()).base_url == ""  # absent field: unset
    monkeypatch.setenv("OPENAI_API_KEY", " env-key ")
    route = resolve_model_route(SimpleNamespace(api_base=None, api_key=None))
    # A null endpoint is NOT "" — see the null-endpoint test below for why.
    assert (route.base_url, route.api_key) == (None, "env-key")


def test_a_null_endpoint_stays_none_so_the_sdk_default_still_applies(monkeypatch, no_network):
    """`api_base:` written with no value parses to None, and one edit to a host config flips
    every agent on the box. The runtime client and embeddings have always passed that None
    through, so the OpenAI SDK applies its own default; `""` fails every call instead. The
    readers that need a string coerce at their own edge."""
    from graph.config import resolve_model_route
    from graph.llm import _build_llm_kwargs, _gateway_client_kwargs
    from graph.model_window import context_window_for

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig.from_dict({"model": {"api_base": None, "api_key": "k", "name": "m"}})
    assert cfg.api_base is None, "a null api_base must survive from_dict for this to mean anything"
    assert resolve_model_route(cfg).base_url is None
    assert _build_llm_kwargs(cfg)["base_url"] is None  # the SDK default, exactly as before
    assert _gateway_client_kwargs(cfg, timeout=5.0)["base_url"] == ""  # coerced at the edge
    assert context_window_for(cfg) is None  # no endpoint to probe; never a crash


def test_padded_legacy_values_are_trimmed_by_every_reader(monkeypatch, no_network):
    """The one alignment: `""` was already "unset" everywhere, but whitespace-only and
    padded values were trimmed by six readers and sent verbatim by the runtime client,
    embeddings, the gateway HTTP client and the egress host. They agree now."""
    from graph.llm import _build_llm_kwargs, _gateway_client_kwargs

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    cfg = LangGraphConfig.from_dict({"model": {"api_base": f"  {LEGACY}  ", "api_key": "   "}})
    kwargs = _build_llm_kwargs(cfg)
    assert (kwargs["base_url"], kwargs["api_key"]) == (LEGACY, "env-key")
    assert _embeddings(cfg, monkeypatch) == (LEGACY, "env-key")
    client = _gateway_client_kwargs(cfg, timeout=1.0)
    assert (client["base_url"], client["headers"]["Authorization"]) == (LEGACY, "Bearer env-key")
    assert _window_probe(cfg, monkeypatch) == (LEGACY, "env-key")


# ── the ratchet: no new direct reads ──────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = (
    "a2a_impl", "events", "graph", "infra", "ingestion", "knowledge", "observability",
    "operator_api", "ops", "plugins", "runtime", "scheduler", "security", "server", "tools", "scripts",
)  # fmt: skip
_DIRECT_READ = re.compile(
    r"\b(?:config|cfg|conf|graph_config|new_config)\.(?:api_base|api_key)\b"
    r"|\bgetattr\s*\(\s*[^,()]+,\s*[\"'](?:api_base|api_key)[\"']"
    # `vars(config).get("api_key")` reads the same field by another road — it slipped past
    # the first version of this ratchet in review.
    r"|\bvars\s*\(\s*[^)]*\)\s*\.\s*get\s*\(\s*[\"'](?:api_base|api_key)[\"']"
)
#: Every direct read of `model.api_base` / `model.api_key` left, and why. The removal
#: slice deletes these; nothing else may add one — resolve through `resolve_model_route`.
_ALLOWED = {
    "graph/config.py": 2,  # resolve_model_route itself
    "graph/config_io.py": 1,  # sync_host_model_layer: a WRITER mirroring into host-config.yaml
    "graph/providers/discovery.py": 2,  # available_model_lanes' synthesized lane (an ADR 0106 floor)
    "runtime/cli.py": 7,  # Hermes seeding: an EXPORT that must not write the env key to disk
    "operator_api/runtime.py": 2,  # /api/runtime status: display
    "graph/model_cli.py": 1,  # `protoagent model list`: display
}


def _code_text(src: str) -> list[str]:
    """Source lines with comments and docstrings removed, tokens space-joined."""
    lines: dict[int, list[str]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.string.lstrip("rbuRBU").startswith(('"""', "'''")):
            continue
        lines.setdefault(tok.start[0], []).append(tok.string)
    return [" ".join(parts).replace(" . ", ".") for _, parts in sorted(lines.items())]


def test_no_new_direct_reads_of_the_retired_endpoint_and_key():
    found: dict[str, int] = {}
    for pkg in _SCANNED:
        for path in sorted((_ROOT / pkg).rglob("*.py")):
            n = sum(len(_DIRECT_READ.findall(line)) for line in _code_text(path.read_text(encoding="utf-8")))
            if n:
                found[path.relative_to(_ROOT).as_posix()] = n
    assert found == _ALLOWED, (
        "A direct read of model.api_base / model.api_key appeared or disappeared. Resolve the "
        "default route through graph.config.resolve_model_route instead; if a listed site was "
        "removed, lower its count here."
    )
