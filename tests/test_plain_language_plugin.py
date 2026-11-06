"""Plain Language plugin — manifest, registration, skills, middleware, and the linter.

The linter is deterministic, so its rules are pinned case by case here. Whether the
core rules actually make replies clearer is a model question; that is what
``plugins/plain_language/eval/ab.py`` measures, not these tests.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from langchain_core.messages import SystemMessage

from graph.plugins.testkit import FakeRegistry, load_plugin
from graph.skills.loader import parse_skill_md

ROOT = Path("plugins/plain_language")

AGENT_SKILLS = {"plain-language-write", "plain-language-edit"}
SLASH_SKILLS = {"plain-review": "plain-review", "paraphrase-test": "paraphrase-test"}


def _pkg():
    return load_plugin(ROOT, "plain_language")


def _registered():
    pkg = _pkg()
    registry = FakeRegistry("plain_language", plugin_dir=ROOT)
    pkg.register(registry)
    return pkg, registry


def _lint():
    return _pkg().lint


def _rules(text: str) -> list[str]:
    return [f.rule for f in _lint().check(text).findings]


# ── packaging ────────────────────────────────────────────────────────────────


def test_manifest_parses_and_declares_no_reach():
    data = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert data["id"] == "plain_language"
    assert data["enabled"] is True
    assert data["version"] and data["min_protoagent_version"]
    assert data["capabilities"] == {"network": [], "filesystem": "none"}
    assert data["config"] == {"always_on": True}
    assert [s["key"] for s in data["settings"]] == ["always_on"]


def test_register_contributes_skills_middleware_tool_and_reader():
    _, registry = _registered()
    assert [Path(p).name for p in registry.skill_dirs] == ["skills"]
    assert len(registry.middlewares) == 1
    assert [t.name for t in registry.tools] == ["plain_language_check"]
    assert [s.name for s in registry.subagents] == ["plain_reader"]
    reader = registry.subagents[0]
    # The reader must not be able to look anything up: it reads as the persona would.
    assert reader.tools == [] and reader.inherit_read_tools is False
    assert not registry.routers and not registry.surfaces


def test_skills_are_loader_valid_and_in_the_right_class():
    files = sorted(ROOT.glob("skills/*/SKILL.md"))
    assert len(files) == len(AGENT_SKILLS) + len(SLASH_SKILLS)
    for path in files:
        art = parse_skill_md(path)
        assert art is not None and art.prompt_template, path
        if art.name in AGENT_SKILLS:
            assert not art.user_only, f"{art.name} must be agent-retrievable"
        else:
            assert art.user_only and art.slash == SLASH_SKILLS[art.name], art.name


def test_slash_tokens_dont_collide_with_core_subagents():
    from graph.subagents.config import SUBAGENT_REGISTRY

    assert not set(SLASH_SKILLS.values()) & set(SUBAGENT_REGISTRY)


# ── middleware ───────────────────────────────────────────────────────────────


def test_core_rules_append_to_a_string_system_message():
    pkg = _pkg()
    out = pkg.with_core_rules(SystemMessage(content="You are an agent."))
    assert out.content[0] == {"type": "text", "text": "You are an agent."}
    assert out.content[-1]["text"].startswith(pkg.core.MARK)


def test_core_rules_are_idempotent_on_block_content():
    pkg = _pkg()
    once = pkg.with_core_rules(SystemMessage(content=[{"type": "text", "text": "base", "cache_control": {"x": 1}}]))
    twice = pkg.with_core_rules(once)
    marked = [b for b in twice.content if b["text"].startswith(pkg.core.MARK)]
    assert len(marked) == 1
    # The cached prefix block is left exactly as it was.
    assert twice.content[0] == {"type": "text", "text": "base", "cache_control": {"x": 1}}


def test_no_system_message_is_left_alone():
    assert _pkg().with_core_rules(None) is None


class _Req:
    def __init__(self, sysmsg):
        self.system_message = sysmsg

    def override(self, **kw):
        new = _Req(kw.get("system_message", self.system_message))
        return new


def test_middleware_respects_always_on_setting():
    pkg, registry = _registered()
    mw = registry.middlewares[0](None)
    req = _Req(SystemMessage(content="base"))

    seen = {}
    mw.wrap_model_call(req, lambda r: seen.setdefault("on", r))
    assert isinstance(seen["on"].system_message.content, list)

    registry.live_config = lambda: {"always_on": False}
    mw.wrap_model_call(req, lambda r: seen.setdefault("off", r))
    assert seen["off"] is req


def test_core_rules_stay_short():
    # The block rides on every model call; keep it a short list, not the whole guide.
    words = len(_pkg().core.CORE_RULES.split())
    assert words < 330, words


# ── linter ───────────────────────────────────────────────────────────────────


def test_plain_text_is_clean():
    text = (
        "Your key expires on 1 March. To keep access, create a new key before then.\n\n"
        "To create a key, follow these steps:\n\n"
        "1. Open Settings.\n2. Choose Keys.\n3. Select New key.\n"
    )
    assert _rules(text) == []


def test_flags_the_mechanical_rules():
    rules = set(
        _rules(
            "In order to utilize the tool, it is very important and/or useful. "
            "See e.g. the guide, or [click here](https://example.com). Applicants shall apply."
        )
    )
    assert {"complex-word", "filler", "and-or", "latin-abbreviation", "vague-link", "shall"} <= rules


def test_flags_hidden_verbs_and_passive_voice():
    rules = _rules("We made a decision to conduct an analysis of the logs. The config was updated by the team.")
    assert rules.count("hidden-verb") >= 2
    assert "passive-voice" in rules


def test_adjective_participles_are_not_passive():
    assert "passive-voice" not in _rules("The plan is based on data. I am interested in it.")


def test_code_and_urls_are_never_flagged():
    text = "Run `utilize --shall` now.\n\n```\nin order to utilize, and/or e.g.\n```\n\nSee https://x.io/very/really.\n"
    assert _rules(text) == []


def test_paragraph_and_sentence_length_limits():
    long_para = " ".join(["We test the code."] * 40)  # 160 words
    assert "long-paragraph" in _rules(long_para)
    huge = " ".join(["We test the code."] * 70)  # 280 words, over the hard 250
    sev = {f.severity for f in _lint().check(huge).findings if f.rule == "long-paragraph"}
    assert sev == {"major"}
    long_sentence = "We " + " ".join(["test"] * 45) + "."
    assert "long-sentence" in _rules(long_sentence)


def test_headings_and_lists():
    text = "# Setup\n\n#### Deep detail here\n\n## Steps\n\n- one\n- two\n"
    rules = _rules(text)
    assert "one-word-heading" in rules  # "Setup"
    assert "heading-depth" in rules  # level 4
    assert rules.count("list-without-lead-in") == 1  # only the list's first item


def test_abbreviations_counted_and_definitions_respected():
    text = "The Plain Writing Act (PWA) applies. The PWA, the XQZ, the RTV and the MNO matter. Use the API."
    rules = _rules(text)
    undefined = [f.excerpt for f in _lint().check(text).findings if f.rule == "undefined-abbreviation"]
    assert sorted(undefined) == ["MNO", "RTV", "XQZ"]  # PWA is defined, API is common
    assert "too-many-abbreviations" in rules


def test_report_metrics_and_format():
    lint = _lint()
    report = lint.check("It is very clear. In order to start, read it.")
    assert report.words == 10
    assert report.major == 0 and report.minor == 2
    assert report.per_100_words == 20.0
    assert report.as_dict()["counts"] == {"complex-word": 1, "filler": 1}
    out = lint.format_report(report)
    assert out.startswith("2 findings in 10 words")
    assert "No plain-language findings" in lint.format_report(lint.check("Read it."))


def test_tool_wraps_the_linter():
    _, registry = _registered()
    out = registry.tools[0].invoke({"text": "Please utilize the form."})
    assert "complex-word" in out and "utilize" in out
