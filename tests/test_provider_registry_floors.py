"""No shipped caller leans on the ADR 0106 legacy-lane floor any more (#3128, path 3).

`split_slot_target` and `available_model_lanes` both fall back to the three legacy lanes
when handed a config with an EMPTY provider registry. That floor exists for configs that
never went through `from_dict`'s migration — a bare `LangGraphConfig()`. The #3128
pre-removal audit found shipped code still producing exactly that (`from_yaml`'s
nothing-to-read branch, `graph.sdk.gateway_client()`'s fallback), so the floor could not
be removed without breaking them.

These tests make the floor's seam (`note_legacy_registry_floor`) fatal and drive every
shipped way a config reaches model routing through it. If a new caller starts depending
on the floor again, the flow that reaches it fails here — and the source scan at the
bottom fails the moment someone writes a bare `LangGraphConfig(...)` in shipped code.

The floor itself is untouched: removing it is the removal PR's job, and
`test_provider_registry.py` still characterizes it for a bare config.
"""

from __future__ import annotations

import ast
import dataclasses
import shutil
from pathlib import Path

import pytest
import yaml

import graph.config as graph_config
from graph.config import LangGraphConfig
from graph.llm import create_llm, split_slot_target
from graph.providers import discovery

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def floor_is_fatal(monkeypatch):
    """Turn the legacy-registry floor into a hard failure for the duration of a test."""

    def _trip(where: str) -> None:
        raise AssertionError(f"a shipped flow reached the ADR 0106 legacy-lane floor via {where}")

    monkeypatch.setattr(graph_config, "note_legacy_registry_floor", _trip)


@pytest.fixture
def no_network(monkeypatch):
    """Lane probes and the context-window lookup are network I/O; none of it is under test."""
    monkeypatch.setattr("graph.config_io.list_gateway_models", lambda base, key, **kw: (["m"], ""), raising=False)
    monkeypatch.setattr(
        discovery, "oauth_status", lambda p: discovery.OAuthStatus(p, False, "", "", "Sign in first.")
    )
    monkeypatch.setattr(discovery, "list_provider_models", lambda provider, cfg: ([], ""))
    monkeypatch.setattr("graph.model_window.context_window_for", lambda cfg, model: None)


def _exercise_routing(cfg: LangGraphConfig) -> None:
    """What the runtime does with a loaded config: parse a qualified slot, build the lead
    model, build a qualified slot, and list the pickers' lanes."""
    assert cfg.providers, "a loaded config must carry a provider registry"
    split_slot_target("gateway:protolabs/coder", cfg)
    split_slot_target("anthropic-oauth:claude-sonnet-5", cfg)
    create_llm(cfg)
    if cfg.provider_by_id("gateway") is not None and cfg.provider_by_id("gateway").base_url:
        create_llm(cfg, model_name="gateway:protolabs/coder")
    discovery.available_model_lanes(cfg)
    discovery.qualified_model_options(cfg)


def _write(path: Path, doc: dict | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc), encoding="utf-8")
    return path


# ── the flows ─────────────────────────────────────────────────────────────────────────


def test_from_yaml_with_nothing_to_read_still_carries_a_registry(tmp_path, floor_is_fatal, no_network):
    """No agent file and no host layer: the branch that used to return a bare `cls()`."""
    cfg = LangGraphConfig.from_yaml(tmp_path / "absent" / "langgraph-config.yaml")
    assert [p.id for p in cfg.providers] == ["gateway"]
    _exercise_routing(cfg)


def test_the_shipped_example_as_a_live_config(tmp_path, floor_is_fatal, no_network):
    """First boot seeds the live config from the example (`ensure_live_config`)."""
    live = tmp_path / "config" / "langgraph-config.yaml"
    live.parent.mkdir(parents=True)
    shutil.copyfile(REPO / "config" / "langgraph-config.example.yaml", live)
    _exercise_routing(LangGraphConfig.from_yaml(live))


@pytest.mark.parametrize("host", [None, "legacy", "registry"])
def test_a_new_workspace_config_on_every_host_shape(tmp_path, monkeypatch, host, floor_is_fatal, no_network):
    """`workspace create` writes `_CONFIG_TEMPLATE`; the box's Host layer may be absent,
    legacy-shaped (what `sync_host_model_layer` mirrors) or registry-shaped."""
    from graph.workspaces.manager import _CONFIG_TEMPLATE

    host_doc = {
        None: None,
        "legacy": {"model": {"name": "protolabs/reasoning", "provider": "openai", "api_base": "https://box/v1"}},
        "registry": {"providers": [{"id": "gateway", "type": "openai-compat", "base_url": "https://box/v1"}]},
    }[host]
    if host_doc is not None:
        monkeypatch.setenv("PROTOAGENT_HOST_CONFIG", str(_write(tmp_path / "host-config.yaml", host_doc)))
    live = _write(tmp_path / "ws" / "config" / "langgraph-config.yaml", _CONFIG_TEMPLATE.format(name="w", id="w"))
    _exercise_routing(LangGraphConfig.from_yaml(live))


def test_the_setup_wizard_shape(tmp_path, floor_is_fatal, no_network):
    """What first-run setup writes today: one connection and a qualified `model.name`."""
    live = _write(
        tmp_path / "config" / "langgraph-config.yaml",
        {
            "providers": [{"id": "gateway", "type": "openai-compat", "base_url": "https://gw/v1"}],
            "model": {"name": "gateway:protolabs/reasoning"},
        },
    )
    _write(tmp_path / "config" / "secrets.yaml", {"providers": {"gateway": "sk-wizard"}})
    _exercise_routing(LangGraphConfig.from_yaml(live))


def test_sdk_gateway_client_before_a_config_is_loaded(monkeypatch, floor_is_fatal):
    """The fallback the #3128 audit named first: `STATE.graph_config or LangGraphConfig()`."""
    from graph import sdk
    from runtime.state import STATE

    seen: list[LangGraphConfig] = []
    monkeypatch.setattr(STATE, "graph_config", None, raising=False)
    monkeypatch.setattr("graph.llm.gateway_client", lambda cfg, **kw: seen.append(cfg) or object())
    sdk.gateway_client()
    assert [p.id for p in seen[0].providers] == ["gateway"]
    assert seen[0].provider_by_id("gateway").base_url == seen[0].api_base


# ── the guard has teeth, and says exactly what is left ──────────────────────────────────


def test_a_bare_config_does_reach_the_floor(floor_is_fatal, no_network):
    """Without this, every test above could pass because the seam was never wired."""
    with pytest.raises(AssertionError, match="split_slot_target"):
        split_slot_target("gateway:protolabs/coder", LangGraphConfig())
    with pytest.raises(AssertionError, match="available_model_lanes"):
        discovery.available_model_lanes(LangGraphConfig())


def test_an_explicitly_empty_registry_is_the_one_shape_still_on_the_floor(floor_is_fatal, no_network):
    """`providers: []` — every connection removed — is a deliberate statement, so the load
    does not migrate it, and the pickers still answer it from the floor. Pinned so the
    removal PR has to decide what an empty registry offers, rather than inherit it."""
    cfg = LangGraphConfig.from_dict({"providers": []})
    assert cfg.providers == []
    with pytest.raises(AssertionError, match="available_model_lanes"):
        discovery.available_model_lanes(cfg)


def test_app_defaults_differs_from_a_bare_config_only_in_its_registry():
    bare, defaults = LangGraphConfig(), LangGraphConfig.app_defaults()
    for f in dataclasses.fields(LangGraphConfig):
        if f.name != "providers":
            assert getattr(defaults, f.name) == getattr(bare, f.name), f.name
    assert bare.providers == []
    assert [(p.id, p.base_url) for p in defaults.providers] == [("gateway", bare.api_base)]


def test_from_yaml_with_nothing_to_read_is_app_defaults(tmp_path):
    assert LangGraphConfig.from_yaml(tmp_path / "none.yaml") == LangGraphConfig.app_defaults()


# ── the structural guard: no new direct construction in shipped code ─────────────────

_SKIP_DIRS = {"tests", ".venv", "venv", "node_modules", "apps", "docs", ".git", "build", "dist", "__pycache__"}


def _shipped_python_files():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts and (rel.parts[0] in _SKIP_DIRS or any(p in _SKIP_DIRS for p in rel.parts)):
            continue
        yield rel, path


def test_no_shipped_code_constructs_a_config_directly():
    """A `LangGraphConfig(...)` call skips the ADR 0106 migration, so its registry is empty
    and model routing answers it from the floor. Shipped code goes through `from_yaml`,
    `from_dict` or `LangGraphConfig.app_defaults()` (`cls(...)` inside those is the seam)."""
    offenders = []
    for rel, path in _shipped_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name == "LangGraphConfig":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "shipped code builds a LangGraphConfig directly — use LangGraphConfig.app_defaults() "
        f"(or from_yaml / from_dict) so it carries a provider registry: {offenders}"
    )
