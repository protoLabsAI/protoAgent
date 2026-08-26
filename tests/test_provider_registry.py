"""The provider registry (ADR 0106) — S1: schema, migration, lookups.

Providers are CONNECTIONS, not a mode: `type` is the kind, `id` is which one, and
several entries may share a type. That is what makes two gateways possible, and it is
what removes the single lead provider every "isn't the current default" message
descended from.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph.config import (
    PROVIDER_TYPE_OPENAI_COMPAT,
    LangGraphConfig,
    Provider,
    valid_provider_id,
)
from graph.llm import _gateway_configured, create_llm, split_slot_target


def _cfg(**doc) -> LangGraphConfig:
    return LangGraphConfig.from_dict(doc)


# ── ids are the stored vocabulary, so they are constrained ────────────────────


@pytest.mark.parametrize("pid", ["gateway", "prod-gateway", "local_vllm", "g1", "openai-codex"])
def test_valid_ids(pid):
    assert valid_provider_id(pid)


@pytest.mark.parametrize("pid", ["", " ", "Bad", "has:colon", "has/slash", "-leading", "has space", "é"])
def test_invalid_ids(pid):
    """A colon would break the `<provider>:<model>` split; a slash collides with gateway
    aliases. Both are excluded so the grammar stays unambiguous by construction."""
    assert not valid_provider_id(pid)


def test_a_malformed_entry_is_dropped_not_fatal():
    """A typo in one connection must not take the agent down."""
    cfg = _cfg(providers=[{"id": "Bad:Id"}, {"id": "ok"}, "not-a-mapping", {"id": "x", "type": "nope"}])
    assert cfg.provider_ids() == ["ok"]


def test_duplicate_ids_keep_the_first():
    cfg = _cfg(providers=[{"id": "g", "base_url": "first"}, {"id": "g", "base_url": "second"}])
    assert [p.base_url for p in cfg.providers] == ["first"]


# ── the thing that was impossible ─────────────────────────────────────────────


def test_two_openai_compatible_gateways():
    cfg = _cfg(
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "label": "Production", "base_url": "https://api/v1"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ]
    )
    assert cfg.provider_ids() == ["prod-gateway", "local-vllm"]
    assert cfg.provider_by_id("local-vllm").base_url == "http://localhost:8000/v1"
    assert cfg.provider_by_id("PROD-GATEWAY").label == "Production"  # lookup is case-insensitive
    assert cfg.provider_by_id("nope") is None


def test_label_is_display_only_and_falls_back_to_the_id():
    assert Provider(id="gateway").display() == "gateway"
    assert Provider(id="gateway", label="Production").display() == "Production"


# ── migration is as close to an identity function as it can be ────────────────


def test_a_legacy_gateway_config_migrates_to_the_gateway_id():
    """The id is literally `gateway` so every stored `gateway:<model>` keeps resolving."""
    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway"]
    p = cfg.provider_by_id("gateway")
    assert (p.type, p.base_url, p.api_key) == (PROVIDER_TYPE_OPENAI_COMPAT, "https://gw/v1", "k")


@pytest.mark.parametrize(
    "slot",
    ["anthropic-oauth:claude-opus-4-6", "openai-codex:gpt-5.5", "gateway:protolabs/coder"],
)
def test_native_slots_survive_a_gateway_only_migration(slot):
    """The regression this nearly shipped.

    The commonest legacy shape — `model.provider: openai` with a gateway — migrates to a
    registry of `[gateway]` alone. If the grammar's whitelist were the registry ALONE, a
    stored `anthropic-oauth:…` aux slot would stop being claimed and get sent to the
    gateway as a bare model id, silently breaking exactly the gateway-lead-plus-native-slots
    mixing the qualified grammar was built for (#2574).
    """
    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway"]
    lane, model = split_slot_target(slot, cfg)
    assert lane == slot.split(":", 1)[0]
    assert model == slot.split(":", 1)[1]


def test_the_legacy_floor_does_not_swallow_an_unknown_prefix():
    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert split_slot_target("bedrock:anthropic.claude", cfg) == ("", "bedrock:anthropic.claude")


@pytest.mark.parametrize("lead", ["anthropic-oauth", "openai-codex"])
def test_a_legacy_native_oauth_config_migrates_under_its_own_lane_id(lead):
    cfg = _cfg(model={"provider": lead, "name": "some-model", "api_base": "", "api_key": ""})
    assert lead in cfg.provider_ids()
    assert cfg.provider_by_id(lead).type == lead


def test_a_legacy_native_provider_with_a_gateway_keeps_both_lanes():
    """Mixing was already possible via qualified slots; the registry must not lose it."""
    cfg = _cfg(model={"provider": "openai-codex", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway", "openai-codex"]


def test_an_explicit_registry_is_not_overwritten_by_migration():
    cfg = _cfg(
        providers=[{"id": "only-mine", "base_url": "https://x/v1"}],
        model={"provider": "openai", "api_base": "https://legacy/v1", "api_key": "k"},
    )
    assert cfg.provider_ids() == ["only-mine"]


def test_a_config_with_no_model_source_at_all_migrates_to_nothing(monkeypatch):
    # An env key IS a model source — migration folds it in — so "nothing configured"
    # means nothing in config AND nothing in the environment.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _cfg(model={"provider": "openai", "api_base": "", "api_key": ""}).providers == []


# ── secrets overlay, exactly as model.api_key behaved ─────────────────────────


def test_a_provider_key_comes_from_secrets_over_inline(tmp_path):
    cfg = LangGraphConfig.from_dict(
        {"providers": [{"id": "gw", "api_key": "inline"}]},
        secrets={"providers": {"gw": "from-secrets"}},
    )
    assert cfg.provider_by_id("gw").api_key == "from-secrets"


def test_as_dict_redacts_the_key_by_default():
    p = Provider(id="gw", base_url="https://x/v1", api_key="shh")
    assert "api_key" not in p.as_dict()
    assert p.as_dict(redact=False)["api_key"] == "shh"


# ── S2: dispatch routes by registered connection, not by a hardcoded lane ──────


def _registry_cfg() -> LangGraphConfig:
    return _cfg(
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1", "api_key": "pk"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1", "api_key": "lk"},
            {"id": "claude", "type": "anthropic-oauth"},
        ],
        model={"name": "prod-gateway:protolabs/reasoning"},
    )


def test_the_grammar_claims_any_registered_id():
    cfg = _registry_cfg()
    assert split_slot_target("local-vllm:qwen3-32b", cfg) == ("local-vllm", "qwen3-32b")
    assert split_slot_target("prod-gateway:protolabs/coder", cfg) == ("prod-gateway", "protolabs/coder")


def test_an_unregistered_prefix_stays_part_of_the_model_name():
    """The rule that keeps `bedrock:anthropic.claude` a model id rather than a route."""
    cfg = _registry_cfg()
    assert split_slot_target("bedrock:anthropic.claude", cfg) == ("", "bedrock:anthropic.claude")
    assert split_slot_target("protolabs/reasoning", cfg) == ("", "protolabs/reasoning")


def test_a_config_with_no_registry_still_speaks_the_legacy_lanes():
    """A bare config (a test, a caller that never loaded YAML) means what it always did."""
    bare = LangGraphConfig()
    assert bare.providers == []
    assert split_slot_target("gateway:protolabs/coder", bare) == ("gateway", "protolabs/coder")
    assert split_slot_target("openai-codex:gpt-5.5", bare) == ("openai-codex", "gpt-5.5")
    assert split_slot_target("prod-gateway:x", bare) == ("", "prod-gateway:x")


def test_two_gateways_build_against_their_own_endpoints():
    """The thing the single api_base/api_key pair made impossible."""
    cfg = _registry_cfg()
    prod = create_llm(cfg, model_name="prod-gateway:protolabs/coder")
    local = create_llm(cfg, model_name="local-vllm:qwen3-32b")
    assert str(prod.openai_api_base) == "https://prod/v1"
    assert str(local.openai_api_base) == "http://localhost:8000/v1"
    assert prod.model_name == "protolabs/coder"
    assert local.model_name == "qwen3-32b"
    # Each carries its OWN key, not the one config-level field.
    assert prod.openai_api_key.get_secret_value() == "pk"
    assert local.openai_api_key.get_secret_value() == "lk"


@pytest.mark.parametrize(
    ("connection_id", "provider_type", "model"),
    [
        ("claude", "anthropic-oauth", "claude-sonnet-5"),
        ("codex", "openai-codex", "gpt-5.6-sol"),
    ],
)
def test_registered_subscription_ids_dispatch_to_their_provider_type(
    monkeypatch, connection_id, provider_type, model
):
    """The operator chooses an id, but the client builder must route on its TYPE.

    This covers the exact add-connection shape: friendly ids such as ``claude`` and
    ``codex`` are not hardcoded lanes, yet must still enter the Anthropic and Codex
    native OAuth pipelines with the unqualified model id.
    """
    cfg = _cfg(
        providers=[{"id": connection_id, "type": provider_type}],
        model={"name": f"{connection_id}:{model}"},
    )
    seen: dict = {}
    sentinel = SimpleNamespace()

    def _build(ptype, _config, *, model_name=None, reasoning_effort=None):
        seen.update(ptype=ptype, model=model_name, effort=reasoning_effort)
        return sentinel

    monkeypatch.setattr("graph.providers.build_native_oauth_llm", _build)
    assert create_llm(cfg) is sentinel
    assert seen == {"ptype": provider_type, "model": model, "effort": None}


def test_a_connection_is_usable_when_it_has_somewhere_to_talk_to(monkeypatch):
    """A key is optional — a local vLLM or Ollama wants none — and is never borrowed.

    "Configured" for a registered connection therefore means it has an endpoint, not that
    some global key exists somewhere else.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _registry_cfg()
    assert _gateway_configured(cfg, cfg.provider_by_id("prod-gateway")) is True

    keyless = _cfg(providers=[{"id": "no-key", "base_url": "https://x/v1"}])
    keyless.api_key = ""
    assert _gateway_configured(keyless, keyless.provider_by_id("no-key")) is True

    nowhere = _cfg(providers=[{"id": "nowhere", "type": "openai-compat"}])
    nowhere.api_key = ""
    assert _gateway_configured(nowhere, nowhere.provider_by_id("nowhere")) is False


def test_a_connection_with_nowhere_to_talk_to_raises_naming_itself(monkeypatch):
    """A keyless endpoint builds (that is normal); one with NO endpoint cannot.

    The error names the connection, because with several registrable "the gateway" no
    longer identifies which one failed.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    keyless = _cfg(providers=[{"id": "local-vllm", "base_url": "http://localhost:8000/v1"}])
    keyless.api_key = ""
    assert create_llm(keyless, model_name="local-vllm:qwen3-32b") is not None

    nowhere = _cfg(providers=[{"id": "nowhere", "type": "openai-compat"}])
    nowhere.api_key = ""
    nowhere.api_base = ""
    with pytest.raises(RuntimeError, match="'nowhere' connection"):
        create_llm(nowhere, model_name="nowhere:some-model")


def test_a_registered_connection_never_borrows_another_credential(monkeypatch):
    """The coupling the registry exists to remove.

    Falling back to `model.api_key` / `OPENAI_API_KEY` for a connection that carries no
    key of its own would send one connection's credential to another's endpoint — a
    local vLLM probed with the production gateway's key. A keyless endpoint is normal
    (vLLM, Ollama), so having somewhere to talk to is what "configured" means.
    """
    from graph.providers.discovery import available_model_lanes

    monkeypatch.setenv("OPENAI_API_KEY", "env-key-must-not-leak")
    cfg = _cfg(
        providers=[
            {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1", "api_key": "pk"},
            {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        ],
        model={"api_key": "legacy-key-must-not-leak"},
    )
    seen: list[tuple[str, str]] = []

    def _probe(base, key, **kw):
        seen.append((base, key))
        return ["m"], ""

    monkeypatch.setattr("graph.config_io.list_gateway_models", _probe, raising=False)
    lanes = {lane["provider"]: lane for lane in available_model_lanes(cfg)}

    assert lanes["local-vllm"]["configured"] is True  # keyless is usable
    probed = dict(seen)
    assert probed["https://prod/v1"] == "pk"
    assert probed["http://localhost:8000/v1"] == ""  # NOT pk, not the legacy key, not env
    assert "legacy-key-must-not-leak" not in probed.values()
    assert "env-key-must-not-leak" not in probed.values()


def test_a_migrated_config_still_offers_a_signed_in_subscription(monkeypatch):
    """The commonest legacy shape migrates to [gateway] alone.

    The runtime still routes `anthropic-oauth:…` (the slot grammar keeps a floor for it),
    so a picker that dropped the lane would offer strictly less than the runtime accepts —
    a signed-in Claude subscription silently disappearing from every model list.
    """
    from graph.providers import discovery

    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway"]  # the shape that caused it

    monkeypatch.setattr("graph.config_io.list_gateway_models", lambda b, k, **kw: (["m"], ""), raising=False)
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, p == "anthropic-oauth", "s", "d", "Sign in")
    )
    monkeypatch.setattr(discovery, "list_provider_models", lambda p, c: (["claude-x"], ""))

    lanes = {lane["provider"]: lane for lane in discovery.available_model_lanes(cfg)}
    assert set(lanes) == {"gateway", "anthropic-oauth", "openai-codex"}
    assert lanes["anthropic-oauth"]["configured"] and lanes["anthropic-oauth"]["models"] == ["claude-x"]
    # ...and the one you can't use is reported with a reason, never omitted.
    assert lanes["openai-codex"]["configured"] is False
    assert "Sign in" in lanes["openai-codex"]["error"]


def test_an_explicit_subscription_entry_is_not_duplicated(monkeypatch):
    from graph.providers import discovery

    cfg = _cfg(providers=[{"id": "claude", "type": "anthropic-oauth"}])
    monkeypatch.setattr(discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, False, "", "", "Sign in"))
    lanes = [lane["provider"] for lane in discovery.available_model_lanes(cfg)]
    # `claude` covers the anthropic-oauth TYPE, so no second entry for it.
    assert lanes.count("anthropic-oauth") == 0
    assert "claude" in lanes and "openai-codex" in lanes


def test_the_build_path_never_sends_one_connection_key_to_another(monkeypatch):
    """The third and worst instance of this coupling — the actual client builder.

    `_build_llm_kwargs` seeds kwargs from the legacy `model.api_base`/`api_key`, so
    merely SKIPPING a blank connection key left the previous credential in place and sent
    it to this endpoint. Discovery and the CRUD route were fixed first; this is the one
    that put a production key on the wire to a local endpoint on every turn.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-LEAK")
    cfg = _cfg(
        providers=[{"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"}],
        model={"name": "local-vllm:qwen3-32b", "api_key": "PROD-GATEWAY-KEY", "api_base": "https://prod/v1"},
    )
    llm = create_llm(cfg, model_name="local-vllm:qwen3-32b")
    assert str(llm.openai_api_base) == "http://localhost:8000/v1"
    sent = llm.openai_api_key.get_secret_value()
    assert sent not in ("PROD-GATEWAY-KEY", "env-key-LEAK")


def test_a_migrated_gateway_still_runs_on_an_env_only_key(monkeypatch):
    """The other half: strictness must not strand an operator who supplies the gateway
    key through OPENAI_API_KEY, so migration folds it into the entry itself."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    cfg = _cfg(model={"name": "protolabs/reasoning", "api_base": "https://prod/v1"})
    assert cfg.provider_by_id("gateway").api_key == "env-key"
    llm = create_llm(cfg, model_name="gateway:protolabs/reasoning")
    assert llm.openai_api_key.get_secret_value() == "env-key"
    assert str(llm.openai_api_base) == "https://prod/v1"


def test_a_keyless_connection_does_not_pick_up_the_env_key_via_the_probe(monkeypatch):
    """`list_gateway_models` has its own OPENAI_API_KEY fallback, which quietly undid the
    isolation its callers had just enforced. A registered connection opts out."""
    from graph.config_io import list_gateway_models

    monkeypatch.setenv("OPENAI_API_KEY", "env-key-LEAK")
    seen: dict = {}

    def _capture(url, headers=None, **kw):
        seen["auth"] = (headers or {}).get("Authorization", "")
        raise RuntimeError("stop here — the header is what matters")

    monkeypatch.setattr("httpx.get", _capture, raising=False)
    list_gateway_models("http://localhost:8000/v1", "", allow_env_key=False)
    assert "env-key-LEAK" not in seen.get("auth", "")


def test_a_duplicate_connection_id_keeps_the_FIRST_key():
    """`_parse_providers` keeps the first entry for a duplicated id, so the key must too —
    otherwise a later duplicate's credential is applied to the first one's endpoint."""
    from graph.config_io import split_secret_updates

    _, secrets = split_secret_updates(
        {
            "providers": [
                {"id": "gw", "base_url": "https://first/v1", "api_key": "first-key"},
                {"id": "gw", "base_url": "https://second/v1", "api_key": "second-key"},
            ]
        }
    )
    assert secrets == {"providers": {"gw": "first-key"}}


def test_a_connection_key_never_reaches_the_legacy_endpoint(monkeypatch):
    """The mirror of the key leak, and the one that survived to round 11.

    Removing the KEY fallback left the BASE_URL fallback: a connection with a key but no
    endpoint of its own built against the legacy `model.api_base`, putting that
    connection's credential in front of the legacy gateway. Both directions of the
    coupling had to go, so an endpoint is what makes a connection usable and neither
    field is ever borrowed.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(
        providers=[{"id": "keyed-no-base", "type": "openai-compat", "api_key": "CONNECTION-KEY"}],
        model={"name": "keyed-no-base:m", "api_base": "https://legacy-gateway/v1", "api_key": "legacy"},
    )
    with pytest.raises(RuntimeError, match="'keyed-no-base' connection"):
        create_llm(cfg, model_name="keyed-no-base:m")
    assert _gateway_configured(cfg, cfg.provider_by_id("keyed-no-base")) is False


def test_a_connection_with_an_endpoint_builds_against_its_own(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(
        providers=[{"id": "mine", "type": "openai-compat", "base_url": "https://mine/v1", "api_key": "mk"}],
        model={"name": "mine:m", "api_base": "https://legacy-gateway/v1", "api_key": "legacy"},
    )
    llm = create_llm(cfg, model_name="mine:m")
    assert str(llm.openai_api_base) == "https://mine/v1"
    assert llm.openai_api_key.get_secret_value() == "mk"


# ── removing the last connection must stay removed ────────────────────────────
#
# Reported live: deleting the gateway reported success, and it was back after a
# refresh. Migration keyed on the registry being EMPTY, and `providers: []` is exactly
# what removing the last connection writes — so the next load treated the operator's
# deliberate "none" as "not migrated yet" and re-created the entry. Both sides were
# telling the truth about different moments.


def test_an_explicitly_empty_registry_is_not_re_migrated():
    cfg = _cfg(providers=[], model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == []


def test_an_absent_key_still_migrates():
    """The other half — a pre-ADR-0106 config must still get its registry."""
    cfg = _cfg(model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert cfg.provider_ids() == ["gateway"]


def test_deleting_the_last_connection_survives_a_reload():
    """End to end, the way the console does it: read, drop, write, reload."""
    cfg = _cfg(
        providers=[{"id": "gateway", "type": "openai-compat", "base_url": "https://gw/v1"}],
        model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"},
    )
    assert cfg.provider_ids() == ["gateway"]
    remaining = [p.as_dict() for p in cfg.providers if p.id != "gateway"]
    reloaded = _cfg(providers=remaining, model={"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"})
    assert reloaded.provider_ids() == []


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        # A registry is a LIST. Absent, null and malformed all mean "nothing was
        # declared" and migrate; only an actual empty list is the operator saying "none".
        # `from_dict` normalizes null values to {} before this runs, so an `is None`
        # check could never see a bare `providers:` line.
        ("absent", "__absent__", ["gateway"]),
        ("null", None, ["gateway"]),
        ("malformed mapping", {}, ["gateway"]),
        ("explicit empty", [], []),
    ],
)
def test_only_an_explicit_empty_list_means_no_connections(label, value, expected):
    doc = {"model": {"provider": "openai", "api_base": "https://gw/v1", "api_key": "k"}}
    if value != "__absent__":
        doc["providers"] = value
    assert _cfg(**doc).provider_ids() == expected


def test_an_id_less_roster_entry_does_not_mask_a_real_member(monkeypatch):
    """The host entry has no workspace; a member sharing its port still does.

    Bailing on the first id-less port match hid the member behind it.
    """
    import types

    from plugins.delegates import autostart as A

    monkeypatch.setattr(
        "graph.fleet.supervisor",
        types.SimpleNamespace(
            status=lambda: [
                {"name": "host", "id": "", "port": 7875, "running": False},
                {"name": "protoEngineer", "id": "pe-ba4c", "port": 7875, "running": False},
            ]
        ),
        raising=False,
    )
    assert A.startable_member("http://127.0.0.1:7875/a2a") == {
        "name": "protoEngineer",
        "id": "pe-ba4c",
        "port": 7875,
    }
