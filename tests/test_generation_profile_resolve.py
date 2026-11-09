"""ADR 0128 S2a — the host-free generation-profile resolver.

Covers the D2 precedence (per-call > subagent > role > global), field-by-field merging,
an unknown name resolving to empty with one warning, no profiles resolving to empty, and
an AST guard that the module imports only ``graph.config`` (the import-linter contract's
strict companion)."""

import ast
import dataclasses
import logging
import pathlib

import pytest

import graph.generation_profile as gp
from graph.config import GenerationProfile, LangGraphConfig
from graph.generation_profile import ResolvedProfile, resolve_profile


def _cfg(**profiles: dict) -> LangGraphConfig:
    """A config whose ``model.profiles`` is ``{name: GenerationProfile(**values)}``."""
    return LangGraphConfig(model_profiles={name: GenerationProfile(**values) for name, values in profiles.items()})


# model.profiles holding one value under three names, one per layer.
_LAYERED = {"hi": {"max_tokens": 3}, "mid": {"max_tokens": 2}, "lo": {"max_tokens": 1}}


@pytest.mark.parametrize(
    "call,subagent,role,expected",
    [
        ("hi", "mid", "lo", 3),  # per-call wins over everything below it
        (None, "mid", "lo", 2),  # subagent wins when there's no per-call
        (None, None, "lo", 1),  # role wins when neither above it is set
        (None, None, None, None),  # nothing named -> inherit global (field stays None)
    ],
)
def test_each_layer_beats_the_one_below(call, subagent, role, expected):
    out = resolve_profile(_cfg(**_LAYERED), call=call, subagent=subagent, role=role)
    assert out.max_tokens == expected


def test_fields_merge_independently_across_layers():
    # Each layer sets a different field (plus max_tokens, which they contest): the result
    # takes each field from the highest layer that set it, not whole-profile replacement.
    cfg = _cfg(
        role={"thinking": "enabled", "max_tokens": 100},
        sub={"reasoning_effort": "high", "max_tokens": 200},
        call={"max_tokens": 300},
    )
    out = resolve_profile(cfg, call="call", subagent="sub", role="role")
    assert out.max_tokens == 300  # per-call wins the contested field
    assert out.reasoning_effort == "high"  # only the subagent set it
    assert out.thinking == "enabled"  # only the role set it
    assert not out.empty


def test_chat_template_kwargs_merge_key_by_key():
    # Unlike the scalar fields, chat_template_kwargs is merged (ADR 0128 D1): a higher
    # layer overrides a key but keeps the keys only a lower layer set.
    cfg = _cfg(
        role={"chat_template_kwargs": {"preserve_thinking": True, "enable_thinking": True}},
        call={"chat_template_kwargs": {"enable_thinking": False}},
    )
    out = resolve_profile(cfg, call="call", role="role")
    assert out.chat_template_kwargs == {"preserve_thinking": True, "enable_thinking": False}


def test_unknown_name_resolves_to_empty_with_one_warning(caplog):
    cfg = _cfg(known={"max_tokens": 10})
    with caplog.at_level(logging.WARNING, logger="graph.generation_profile"):
        out = resolve_profile(cfg, call="typo-not-defined")
    assert out.empty
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "typo-not-defined" in warnings[0].getMessage()


def test_no_profiles_defined_resolves_to_empty():
    # No model.profiles at all: a named layer is unknown, so the result is empty and the
    # call keeps the global block — never raises.
    out = resolve_profile(LangGraphConfig(), call="routing", subagent="x", role="y")
    assert out.empty


def test_no_layers_named_resolves_to_empty():
    assert resolve_profile(_cfg(routing={"max_tokens": 4096})).empty


def test_resolved_profile_is_frozen():
    out = resolve_profile(LangGraphConfig())
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.max_tokens = 1  # type: ignore[misc]


def test_resolved_profile_fields_mirror_generation_profile():
    # The view create_llm consumes must carry exactly the knobs a profile can set.
    assert {f.name for f in dataclasses.fields(ResolvedProfile)} == {
        f.name for f in dataclasses.fields(GenerationProfile)
    }


# The first-party root packages (pyproject [tool.importlinter] root_packages). A host-free
# leaf must reach into none of them except graph.config.
_ROOT_PACKAGES = {
    "a2a_impl", "deck", "events", "graph", "infra", "ingestion", "knowledge",
    "observability", "operator_api", "ops", "runtime", "scheduler", "security",
    "server", "tools",
}


def test_module_imports_only_graph_config():
    """AST guard: the sole first-party import is ``graph.config`` (the strict form of the
    import-linter contract, which can't name ``graph`` itself as forbidden)."""
    tree = ast.parse(pathlib.Path(gp.__file__).read_text(encoding="utf-8"))
    first_party: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _ROOT_PACKAGES:
                    first_party.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.split(".")[0] in _ROOT_PACKAGES:
                first_party.add(module)
    assert first_party == {"graph.config"}
