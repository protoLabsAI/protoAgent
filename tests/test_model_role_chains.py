"""ADR 0133 D1/D2/D5/D6 — model-role fields accept a scalar or an ordered chain, and
``graph.config.role_chain`` resolves each role's effective chain: the D2 inheritance for a
blank role, its exceptions, and the gateway-alias coherence drop.

A bare ``LangGraphConfig(**kw)`` is used throughout: it skips ``from_dict`` (and its scalar
slot reconcile), so list values survive verbatim — exactly what ``role_chain`` reads.
"""

import logging

import pytest

from graph.config import LangGraphConfig, role_chain


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    # PROTOAGENT_MODEL would overwrite model_name in __post_init__; OPENAI_API_KEY would make
    # the native-provider coherence check see a gateway key. Both must be absent here.
    monkeypatch.delenv("PROTOAGENT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _cfg(**kw) -> LangGraphConfig:
    return LangGraphConfig(**kw)


# ── D1: scalar or list; the main chain folds in the legacy fallback tail ──────


def test_main_chain_scalar():
    assert role_chain(_cfg(model_name="a"), "main") == ["a"]


def test_main_chain_list_plus_fallback_tail():
    cfg = _cfg(model_name=["a", "b"], routing_fallback_models=["c"])
    assert role_chain(cfg, "main") == ["a", "b", "c"]


def test_main_chain_scalar_plus_fallback_tail():
    cfg = _cfg(model_name="a", routing_fallback_models=["b", "c"])
    assert role_chain(cfg, "main") == ["a", "b", "c"]


# ── D2: a set chain is exactly what's listed ──────────────────────────────────


def test_set_chain_is_exactly_the_list():
    cfg = _cfg(model_name="m", aux_model=["x", "y"], compaction_model=["c1", "c2"])
    assert role_chain(cfg, "aux") == ["x", "y"]
    assert role_chain(cfg, "compaction") == ["c1", "c2"]


def test_single_element_list_stays_a_chain_of_one():
    assert role_chain(_cfg(model_name=["solo"]), "main") == ["solo"]


# ── D2: a blank role inherits its parent's whole chain ────────────────────────


def test_blank_aux_inherits_the_whole_main_chain():
    cfg = _cfg(model_name=["a", "b"], routing_fallback_models=["c"], aux_model="")
    assert role_chain(cfg, "aux") == ["a", "b", "c"]


def test_blank_specific_role_falls_through_aux_to_main():
    # compaction / goal_eval blank → aux's chain …
    cfg = _cfg(model_name="m", aux_model=["x", "y"])
    assert role_chain(cfg, "compaction") == ["x", "y"]
    assert role_chain(cfg, "goal_eval") == ["x", "y"]
    # … and when aux is also blank, through to main.
    bare = _cfg(model_name="m")
    assert role_chain(bare, "compaction") == ["m"]
    assert role_chain(bare, "goal_eval") == ["m"]


# ── D2 exceptions ─────────────────────────────────────────────────────────────


def test_blank_image_describe_inherits_main_chain():
    off = _cfg(model_name=["m", "backup"], aux_model="x", image_describe_model="")
    assert role_chain(off, "image_describe") == ["m", "backup"]
    on = _cfg(model_name="m", image_describe_model="vision")
    assert role_chain(on, "image_describe") == ["vision"]


def test_blank_drift_judge_and_memory_select_inherit_aux():
    cfg = _cfg(model_name="m", aux_model="x")
    assert role_chain(cfg, "soul_drift_judge") == ["x"]
    assert role_chain(cfg, "memory_select") == ["x"]
    # A set chain still wins over the inherited aux.
    pinned = _cfg(model_name="m", aux_model="x", memory_select_model=["s"])
    assert role_chain(pinned, "memory_select") == ["s"]


# ── D5/D6: harvest → aux, facts → harvest, gist → aux ─────────────────────────


def test_memory_passes_default_inheritance():
    cfg = _cfg(model_name="m", aux_model="aux")
    assert role_chain(cfg, "memory_harvest") == ["aux"]
    assert role_chain(cfg, "memory_facts") == ["aux"]  # facts → harvest(blank) → aux
    assert role_chain(cfg, "memory_gist") == ["aux"]


def test_memory_facts_inherits_harvest_not_aux_directly():
    cfg = _cfg(model_name="m", aux_model="aux", memory_harvest_model=["h1", "h2"])
    assert role_chain(cfg, "memory_facts") == ["h1", "h2"]
    # gist still inherits aux, not harvest.
    assert role_chain(cfg, "memory_gist") == ["aux"]


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        role_chain(_cfg(), "not_a_role")


# ── coherence: an incoherent entry is dropped with a log line ─────────────────


def test_incoherent_gateway_alias_is_dropped_with_a_log_line(caplog):
    # A native-OAuth provider with no gateway key: a bare '/'-alias in a chain can't route,
    # so it is dropped (the same incoherence from_dict reconciles the scalar slots for).
    cfg = _cfg(model_provider="anthropic-oauth", api_key="", model_name=["claude-good", "protolabs/reasoning"])
    with caplog.at_level(logging.WARNING, logger="protoagent.config"):
        chain = role_chain(cfg, "main")
    assert chain == ["claude-good"]
    assert any("dropping incoherent model" in r.getMessage() and "main[1]" in r.getMessage() for r in caplog.records)


def test_chain_emptied_by_coherence_drop_is_treated_as_blank_and_inherits():
    # aux is ALL incoherent aliases → emptied → treated as blank → inherits the main chain.
    cfg = _cfg(
        model_provider="anthropic-oauth",
        api_key="",
        model_name="claude-main",
        aux_model=["protolabs/a", "protolabs/b"],
    )
    assert role_chain(cfg, "aux") == ["claude-main"]


def test_coherent_provider_keeps_gateway_aliases():
    # The default openai provider is NOT native-OAuth, so nothing is dropped and a gateway
    # alias is perfectly routable.
    cfg = _cfg(model_name=["protolabs/reasoning", "protolabs/fast"])
    assert role_chain(cfg, "main") == ["protolabs/reasoning", "protolabs/fast"]


# ── Runtime readers tolerate a list-valued slot (ADR 0133 M1 crash fixes) ─────
#
# M1 made the slots list-capable. These readers still return a single primary id (chain-
# aware fan-out is a later milestone), but they must not crash on the old `.strip()` now
# that a slot can be a list.


def test_resolve_aux_model_takes_the_primary_of_a_list_slot():
    from graph.agent import _resolve_aux_model

    # routing.aux_model as a chain → its primary
    assert _resolve_aux_model(_cfg(model_name="m", aux_model=["fast", "slow"])) == "fast"
    # a list `specific` override (compaction passes config.compaction_model) wins, primary used
    assert _resolve_aux_model(_cfg(model_name="m", aux_model="aux"), ["comp1", "comp2"]) == "comp1"
    # both blank → None (→ the main model), unchanged
    assert _resolve_aux_model(_cfg(model_name="m")) is None


def test_resolve_claude_model_name_resolves_the_primary_of_a_list_lead():
    from graph.providers.anthropic_oauth import resolve_claude_model_name

    cfg = _cfg(model_provider="anthropic-oauth", api_key="", model_name=["claude-opus-4-6", "claude-sonnet-4-5"])
    assert resolve_claude_model_name(cfg) == "claude-opus-4-6"
    # an override chain still wins over the lead
    assert resolve_claude_model_name(cfg, ["claude-haiku-4-5"]) == "claude-haiku-4-5"


def test_resolve_claude_model_name_rejects_an_all_alias_list_lead():
    from graph.providers.anthropic_oauth import resolve_claude_model_name

    # Every entry is a '/'-gateway alias — meaningless to Anthropic. The primary trips the
    # existing lead-coherence guard (a clear RuntimeError), not a `.strip()` crash on a list.
    cfg = _cfg(model_provider="anthropic-oauth", api_key="", model_name=["protolabs/reasoning"])
    with pytest.raises(RuntimeError):
        resolve_claude_model_name(cfg)


def test_build_codex_llm_builds_against_the_primary_of_a_list_lead(monkeypatch):
    # openai-codex: a list-valued model.name reaches build_codex_llm as config.model_name
    # (create_llm passes model_name=None for the lead). The builder resolves the chain's
    # PRIMARY id, not a `.strip()` crash on the list.
    import graph.providers.openai_codex as ocx
    from graph.providers.oauth import CodexOAuthCreds

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(access_token="t", account_id="a", base_url="b", source="s"),
    )
    llm = ocx.build_codex_llm(_cfg(model_provider="openai-codex", model_name=["gpt-5-codex", "gpt-5"]))
    assert llm.model_name == "gpt-5-codex"


def test_context_window_for_probes_the_primary_of_a_list_lead(monkeypatch):
    # graph.model_window.context_window_for is a single-model probe; a list-valued
    # config.model_name resolves on its PRIMARY entry instead of crashing on `.strip()`.
    import httpx

    from graph import model_window

    model_window.reset_window_cache()

    def fake_get(url, headers=None, timeout=None):
        class _R:
            status_code = 200

            def json(self):
                return {"data": [{"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}}]}

        return _R()

    monkeypatch.setattr(httpx, "get", fake_get)
    cfg = _cfg(model_name=["protolabs/smart", "protolabs/fast"])
    assert model_window.context_window_for(cfg) == 196608
    model_window.reset_window_cache()
