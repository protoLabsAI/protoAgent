"""An imported agent's model traffic goes where the SOURCE agent's did, with the same key (#3128).

Kept as regression tests from the adversarial review of #3521, whose first head broke both
halves of that in the registry-shape snapshot: it re-derived `model.provider` from the lead's
connection, re-routing bare slots between the gateway and a subscription (A, B, C), and it
credited the retired `model.api_key` to the lead's custom connection while pointing the
retiring readers at that connection's endpoint (D, E, G). Each case passed on the pre-#3128
verbatim export/import (6f828183) and must keep passing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graph.config import LangGraphConfig
from graph.llm import create_llm
from graph.snapshot_import import apply_snapshot, inspect_snapshot
from graph.snapshot_op import build_snapshot

KEY = "sk-" + "livekey" + "q" * 24
SECRET_KEYS = (("model", "api_key"),)

BOX = "https://box.example/v1"
SRC = "https://src.example/v1"
LOCAL = "http://127.0.0.1:8080/v1"

# The Host shape the PR's own matrix uses ("registry-host").
REGISTRY_HOST = {
    "model": {"api_base": BOX},
    "providers": [{"id": "gateway", "type": "openai-compat", "base_url": BOX}],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(tmp_path / "no-host.yaml"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("graph.model_window.context_window_for", lambda cfg, model: None)
    monkeypatch.setattr(
        "graph.providers.build_native_oauth_llm",
        lambda provider, config, *, model_name=None, reasoning_effort=None: SimpleNamespace(
            native=provider, model=model_name or config.model_name
        ),
    )
    return tmp_path, monkeypatch


def _route(cfg, model_name=None):
    llm = create_llm(cfg, model_name=model_name)
    if hasattr(llm, "native"):
        return ("native", llm.native, llm.model)
    key = llm.openai_api_key.get_secret_value() if llm.openai_api_key else ""
    return ("gateway", str(llm.openai_api_base), key, llm.model_name)


def _meaning(cfg, aux):
    return {
        "lead": _route(cfg),
        "aux": _route(cfg, aux) if aux else None,
        # what embeddings / gateway_client / transcription / the egress auto-allow read
        "legacy_readers": (cfg.api_base, cfg.api_key),
    }


def _roundtrip(env, layer, secrets, host=None):
    tmp_path, monkeypatch = env
    if host is not None:
        hf = tmp_path / "host-config.yaml"
        hf.write_text(yaml.safe_dump(host))
        monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hf))
    src = tmp_path / "src" / "config"
    src.mkdir(parents=True)
    (src / "langgraph-config.yaml").write_text(yaml.safe_dump(layer))
    (src / "secrets.yaml").write_text(yaml.safe_dump(secrets))
    source = LangGraphConfig.from_yaml(src / "langgraph-config.yaml")
    snap = build_snapshot(
        config_yaml=src / "langgraph-config.yaml",
        soul_path=src / "SOUL.md",
        plugins_lock=tmp_path / "src" / "plugins.lock",
        secrets_yaml=src / "secrets.yaml",
        agent_name="vera",
        secret_key_paths=SECRET_KEYS,
        plugin_requirements=[],
    )
    plan = inspect_snapshot(snap.data)
    # The operator supplies, for every credential the plan asks for, the value the source had.
    supplied = {r["name"]: KEY for r in plan.required_secrets if r.get("was_set")}
    res = apply_snapshot(snap.data, name="vera-2", acknowledged=True, install=False, secrets=supplied)
    imported = LangGraphConfig.from_yaml(Path(res.path) / "config" / "langgraph-config.yaml")
    return source, imported, plan


_AUX = [None]


def test_A_native_primary_in_model_name_bare_slot_flips_gateway_to_subscription(env):
    """Registry-way subscription lead: model.provider is ui_hidden since ADR 0106, so the lead
    is a qualified model.name and model.provider is absent (default "openai")."""
    aux = _AUX[0] = "gpt-5-mini"
    layer = {
        "providers": [{"id": "anthropic-oauth", "type": "anthropic-oauth"}],
        "model": {"name": "anthropic-oauth:claude-sonnet-4-5"},
        "routing": {"aux_model": aux},
    }
    source, imported, _ = _roundtrip(
        env, layer, {"model": {"api_key": KEY}, "providers": {"gateway": KEY}}, host=REGISTRY_HOST
    )
    assert _meaning(imported, aux) == _meaning(source, aux)


def test_B_LEGACY_native_lead_with_gateway_qualified_primary_flips_subscription_to_gateway(env):
    """Legacy-shaped source (model.provider SET) -- the direction the PR description omits."""
    aux = _AUX[0] = "claude-haiku-4-5"
    layer = {
        "model": {"provider": "anthropic-oauth", "name": "gateway:protolabs/reasoning", "api_base": SRC},
        "routing": {"aux_model": aux},
    }
    source, imported, _ = _roundtrip(env, layer, {"model": {"api_key": KEY}})
    assert _meaning(imported, aux) == _meaning(source, aux)


def test_C_LEGACY_gateway_lead_with_native_qualified_primary_flips_gateway_to_subscription(env):
    aux = _AUX[0] = "gpt-5-mini"
    layer = {
        "model": {"provider": "openai", "name": "anthropic-oauth:claude-sonnet-4-5", "api_base": SRC},
        "routing": {"aux_model": aux},
    }
    source, imported, _ = _roundtrip(env, layer, {"model": {"api_key": KEY}})
    assert _meaning(imported, aux) == _meaning(source, aux)


def test_D_custom_primary_is_credited_with_the_legacy_gateway_key(env):
    """A member on a box gateway whose lead runs on its own keyless local connection. The
    legacy model.api_key authenticates the BOX gateway (model.api_base from the Host); it
    feeds no registered connection (`_parse_providers` reads only secrets.providers)."""
    aux = _AUX[0] = "protolabs/fast"
    layer = {
        "providers": [{"id": "local", "type": "openai-compat", "base_url": LOCAL}],
        "model": {"name": "local:qwen3"},
        "routing": {"aux_model": aux},
    }
    source, imported, plan = _roundtrip(env, layer, {"model": {"api_key": KEY}}, host=REGISTRY_HOST)
    assert source.provider_by_id("local").api_key == ""  # keyless on the source
    asked = [r["name"] for r in plan.required_secrets if r.get("was_set")]
    assert "providers.local" not in asked, "export claims the keyless `local` connection had a key"
    assert imported.provider_by_id("local").api_key == "", "the box-gateway key was filed as `local`'s key"
    assert _meaning(imported, aux) == _meaning(source, aux)


def test_E_bridge_moves_legacy_readers_off_the_host_gateway_even_with_no_key(env):
    aux = _AUX[0] = "protolabs/fast"
    layer = {
        "providers": [{"id": "local", "type": "openai-compat", "base_url": LOCAL}],
        "model": {"name": "local:qwen3"},
        "routing": {"aux_model": aux},
    }
    source, imported, _ = _roundtrip(env, layer, {}, host=REGISTRY_HOST)
    assert imported.api_base == source.api_base  # embeddings / gateway_client / transcription endpoint


def test_F_the_PR_matrix_host_env_is_really_loaded(env):
    """Guard against the PR's host-matrix test being vacuous."""
    tmp_path, monkeypatch = env
    hf = tmp_path / "h.yaml"
    hf.write_text(yaml.safe_dump(REGISTRY_HOST))
    monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(hf))
    from graph.config import _load_host_layer

    assert _load_host_layer().get("providers")


def test_G_old_snapshot_model_api_key_alias_is_filed_as_a_custom_connections_key(env):
    """A pre-#3128 snapshot (config verbatim, gateway key named `model.api_key`) of a registry
    agent whose lead runs on a keyless custom connection, imported the documented old way:
    `--secret model.api_key=...`."""
    import io
    import zipfile

    from graph.snapshot_op import SNAPSHOT_MANIFEST

    manifest = {
        "snapshot_version": 1,
        "kind": "agent-snapshot",
        "agent": {"name": "vera"},
        "plugins": [],
        "config": {"providers": [{"id": "local", "type": "openai-compat", "base_url": LOCAL}], "model": {"name": "local:qwen3"}},
        "soul": None,
        "required_secrets": [{"name": "model.api_key", "kind": "config", "description": "", "was_set": True}],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(SNAPSHOT_MANIFEST, yaml.safe_dump(manifest))
    res = apply_snapshot(buf.getvalue(), name="old-1", acknowledged=True, install=False, secrets={"model.api_key": KEY})
    cfg = LangGraphConfig.from_yaml(Path(res.path) / "config" / "langgraph-config.yaml")
    assert cfg.provider_by_id("local").api_key != KEY, "`--secret model.api_key` was filed as the `local` connection's key"
