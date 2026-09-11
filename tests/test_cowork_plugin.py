"""Tests for the cowork plugin — the knowledge-work skill pack (ADR 0083).

Cowork is bundled into core under ``plugins/cowork/`` (#3450, superseding the
retired ``cowork-plugin`` repo), so ``ROOT`` anchors there off the repo root
rather than the test's parent dir — the same shape as
``tests/test_artifact_plugin.py``.

This is the standalone repo's whole suite, ported, plus the checks that only
became possible once the pack lives with the host it runs on:

* the verifier now runs against the **real** ``graph.goals.types.VerifyResult``
  instead of a hand-stubbed three-field stand-in, and against the real config's
  fence helpers;
* the skills' tool names are checked against the tools that actually exist
  in-tree — the drift that was structurally invisible across two repos;
* ``plugins/cowork/__init__.py`` is held to its host-free import discipline
  (``graph.goals.types`` stays inside the verifier), which is what let the
  standalone suite run with no host and what keeps this file cheap.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import types
from pathlib import Path

import pytest
import yaml

from graph.plugins.testkit import FakeRegistry, load_plugin
from graph.skills.loader import parse_skill_md

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "cowork"
SKILLS = ROOT / "skills"

# The retired repo the bundled manifest supersedes, and its last release. The bundled
# version must stay STRICTLY above that: a copy that loses its plugins.lock row becomes
# untracked, and an untracked copy that isn't older than the bundled one wins (#1574).
RETIRED_REPO = "https://github.com/protoLabsAI/cowork-plugin"
LAST_STANDALONE_RELEASE = (0, 3, 1)

EXPECTED_SKILLS = {
    "docx",
    "xlsx",
    "pptx",
    "pdf",
    "schedule",
    "consolidate-memory",
    "writing-voice",
    "setup-cowork",
    "daily-brief",
    "drop-folder",
}


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))


def _plugin():
    """The plugin package, loaded the way the host loads it."""
    return load_plugin(ROOT, "cowork")


@pytest.fixture
def registry():
    return FakeRegistry(plugin_id="cowork", plugin_dir=ROOT)


# ── manifest: identity, the supersedes contract, and the version floor ─────────


def test_identity_and_trust_defaults():
    m = _manifest()
    assert m["id"] == "cowork"
    # The folder has to be named for the id (guard test in tests/test_plugin_supersedes.py),
    # and this is the plugin that move rule was written for.
    assert ROOT.name == m["id"]
    # Bundled but OPT-IN, unlike notes/docs/artifact/craft: ten skills in every agent's
    # index is the wrong default — the Cowork archetype turns the pack on explicitly.
    assert m["enabled"] is False
    assert isinstance(m["config_section"], str)
    # One config key, declared with a default so Settings can render it.
    assert m["config"] == {"output_dir": ""}


def test_supersedes_names_the_retired_standalone_repo():
    """The field that lets an already-installed git copy stand down in favour of this
    one, keeping the id (and so `plugins.enabled`, the config section and every
    archetype's enable list) intact — #3445."""
    assert _manifest()["supersedes"] == [RETIRED_REPO]


def test_bundled_version_is_above_every_standalone_release():
    version = tuple(int(part) for part in str(_manifest()["version"]).split("."))
    assert version > LAST_STANDALONE_RELEASE, (
        f"bundled cowork {version} must be above the retired repo's last release "
        f"{LAST_STANDALONE_RELEASE} — an untracked copy that isn't older wins (#1574)"
    )


def test_no_min_protoagent_version():
    """It ships WITH the host now: there is no host it could be too new for, and a
    stale floor would only ever refuse to load the copy that came with the release."""
    assert "min_protoagent_version" not in _manifest()


def test_document_skill_deps_declared():
    # requires_pip entries may be a plain string or the optional-tier mapping
    # {pkg: ..., optional: true} (protoAgent #1954) — normalize to names either way.
    pips = _manifest()["requires_pip"]
    names = {e["pkg"] if isinstance(e, dict) else e for e in pips}
    for pkg in ("python-docx", "openpyxl", "python-pptx", "pypdf", "reportlab"):
        assert pkg in names
    # No `scope:` on any of them, deliberately: the skills import these INSIDE
    # execute_code, which on the desktop app is the managed-runtime child, not the host.
    assert all(not isinstance(e, dict) or "scope" not in e for e in pips)


def test_requires_pip_matches_the_frozen_desktop_document_baseline():
    """ADR 0092 / ADR 0094 D3: the desktop app freezes this exact stack in, which is
    what satisfies `requires_pip` there (and is the managed runtime's baseline). If a
    dep is added here without being added there, cowork silently stops working on the
    desktop app — the failure this test exists to make loud."""
    baseline = (REPO / "apps" / "desktop" / "sidecar" / "requirements-docs.txt").read_text(encoding="utf-8")
    frozen = {
        re.split(r"[<>=!~ ]", line, maxsplit=1)[0].strip()
        for line in baseline.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    declared = {e["pkg"] if isinstance(e, dict) else e for e in _manifest()["requires_pip"]}
    assert declared <= frozen, (
        f"declared but not frozen into the desktop sidecar: {sorted(declared - frozen)} — "
        "add them to apps/desktop/sidecar/requirements-docs.txt"
    )


def test_capabilities_are_honest():
    # filesystem went none -> scoped in v0.3.0: the folder_changed verifier
    # lists (never reads) files inside the operator's fenced work folders.
    caps = _manifest()["capabilities"]
    assert caps["network"] == [] and caps["filesystem"] == "scoped"


def test_the_pack_contributes_no_tools_routes_or_surfaces(registry):
    """The manifest's claim, checked against what register() actually does."""
    m = _manifest()
    for field in ("views", "emits", "mcp_servers", "secrets", "public_paths"):
        assert field not in m, f"manifest declares {field} — the pack is skills + one verifier"
    _plugin().register(registry)
    assert not registry.tools and not registry.routers and not registry.surfaces
    assert not registry.subagents and not registry.mcp_servers and not registry.chat_commands


# ── register(): the seams the pack wires, and the failure modes it survives ────


def test_register_contributes_skills(registry):
    _plugin().register(registry)
    assert [Path(p).name for p in registry.skill_dirs] == ["skills"]


def test_register_contributes_folder_changed_verifier(registry):
    _plugin().register(registry)
    fn = registry.verifiers["cowork:folder_changed"]
    assert callable(fn)
    assert registry.verifier_meta["cowork:folder_changed"]["description"]


def test_register_survives_a_broken_registry():
    class Broken:
        config = {}

        def register_skill_dir(self, path):
            raise RuntimeError("boom")

    _plugin().register(Broken())  # must not raise


def test_register_survives_a_host_without_verifiers():
    # An older host registry has no register_goal_verifier — skills must
    # still land and register() must not raise.
    class OldHost:
        config = {}

        def __init__(self):
            self.skill_dirs = []

        def register_skill_dir(self, path):
            self.skill_dirs.append(path)

    old = OldHost()
    _plugin().register(old)
    assert old.skill_dirs == ["skills"]


HOST_PACKAGES = frozenset({"graph", "knowledge", "server", "operator_api", "tools", "infra", "runtime", "events"})


def _isolated_bundled_root(tmp_path, monkeypatch) -> Path:
    """A plugin root holding ONLY this pack, so the real loader discovers it without
    booting every other bundled plugin."""
    from graph.plugins import loader

    root = tmp_path / "bundled"
    root.mkdir()
    (root / "cowork").symlink_to(ROOT)
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [root])
    return root


def test_the_real_loader_discovers_the_pack_and_its_verifier(tmp_path, monkeypatch):
    """End to end through the host's own loader — the check the standalone repo could
    never run: discovery, module import, register(), and the skill dir resolved to a
    real path with all ten skills in it."""
    from graph.config import LangGraphConfig
    from graph.plugins import loader

    _isolated_bundled_root(tmp_path, monkeypatch)
    res = loader.load_plugins(LangGraphConfig(plugins_enabled=["cowork"]))

    (skill_dir,) = res.skill_dirs
    assert {p.parent.name for p in Path(skill_dir).glob("*/SKILL.md")} == EXPECTED_SKILLS
    assert "cowork:folder_changed" in res.goal_verifiers
    assert res.goal_verifier_meta["cowork:folder_changed"]["plugin_id"] == "cowork"
    assert not res.tools and not res.routers


def test_the_pack_stays_off_until_it_is_enabled(tmp_path, monkeypatch):
    """`enabled: false` in the manifest is the whole reason ten skills don't land in
    every agent's index the moment this version ships."""
    from graph.config import LangGraphConfig
    from graph.plugins import loader

    _isolated_bundled_root(tmp_path, monkeypatch)
    res = loader.load_plugins(LangGraphConfig())
    assert not res.skill_dirs and not res.goal_verifiers


def test_the_module_imports_host_free():
    """``graph.goals.types`` must stay imported INSIDE the verifier. In-tree that import
    would resolve anyway, which is exactly why it needs a guard: the discipline is what
    lets the pack load on a host that predates goal verifiers, and what keeps this file
    able to drive register() against a fake registry with no host wired up."""
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:  # module level only — a lazy import lives inside a function
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    hosty = sorted(name for name in imported if name.split(".")[0] in HOST_PACKAGES)
    assert not hosty, f"host imports at module level: {hosty} — keep them inside the function that needs them"


# ── the skills: loader-valid, licence-clean, and naming tools that exist ───────


def test_expected_skill_set():
    assert {p.name for p in SKILLS.iterdir() if p.is_dir()} == EXPECTED_SKILLS


def test_every_skill_meets_the_loader_contract():
    """Parsed by the host's REAL skill loader (ADR 0060), not a stand-in parser."""
    for d in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        path = d / "SKILL.md"
        artifact = parse_skill_md(path)
        assert artifact is not None, f"{path} failed to parse"
        assert artifact.name == d.name, f"{d.name}: frontmatter name must match the folder"
        assert artifact.description, f"{d.name}: description missing"
        assert artifact.prompt_template.strip(), f"{d.name}: empty body"
        # The loader TRUNCATES an over-long description rather than failing, so check the
        # source text too — a silently clipped trigger list is a silently weaker skill.
        raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        assert len(raw["description"]) <= 1024, f"{d.name}: description over the 1024-char cap"


def test_setup_skill_is_slash_invocable():
    artifact = parse_skill_md(SKILLS / "setup-cowork" / "SKILL.md")
    assert artifact is not None
    assert artifact.user_facing is True
    assert artifact.slash == "setup-cowork"


def test_no_anthropic_licensed_material_vendored():
    # ADR 0083 D3: Anthropic's Cowork skills are all-rights-reserved and must
    # never be copied into this pack. Tripwire, not a formality.
    for path in SKILLS.rglob("*"):
        assert path.name != "LICENSE.txt", f"vendored license file at {path}"
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "Anthropic, PBC" not in text, f"Anthropic-licensed text in {path}"


# The core tools the skills direct the agent to call. Kept explicit because a skill can
# name a tool in prose (`daily-brief`'s description says load_skill) where no backtick
# sweep would find it.
SKILL_TOOL_CONTRACT = frozenset(
    {
        "execute_code",  # docx / xlsx / pptx / pdf — the runtime every document skill drives
        "save_file_artifact",  # …and the versioned download card it registers the file as
        "show_artifact",  # daily-brief renders the brief as an HTML artifact, never chat text
        "load_skill",  # daily-brief's description tells the agent to load it first
        "memory_recall",
        "memory_ingest",
        "memory_list",
        "memory_stats",
        "forget_memory",  # consolidate-memory's merge/retire pass
        "save_skill",  # writing-voice saves the voice profile as a skill
        "schedule_task",
        "list_schedules",
        "cancel_schedule",  # schedule
        "create_watch",
        "list_watches",
        "clear_watch",  # drop-folder
    }
)

# Backticked identifiers in the skills that are deliberately NOT tool names: tool
# PARAMETERS, config keys, manifest fields, and third-party library symbols. Anything
# backticked and not here has to be a real in-tree tool — that is the drift guard.
NOT_TOOL_NAMES = frozenset(
    {
        "check",  # create_watch params…
        "check_args",
        "run_prompt",
        "interval_s",
        "expires_in_s",
        "artifact_id",  # save_file_artifact param
        "output_dir",  # this plugin's config key
        "requires_pip",  # manifest field
        "csv",  # stdlib / libraries the skills' execute_code snippets use
        "openpyxl",
        "load_workbook",
        "extract_text",
    }
)

_WATCH_ARGS_THE_SKILLS_NAME = frozenset({"check", "check_args", "run_prompt", "interval_s", "expires_in_s"})


def _in_tree_tool_names() -> set[str]:
    """Every ``@tool``-decorated tool name in the tree — ``@tool`` and
    ``@tool("explicit_name", …)`` both, since the explicit form is what
    ``execute_code`` uses. AST, not imports: the point is a cheap, total sweep."""
    roots = ("tools", "graph", "plugins", "knowledge", "scheduler", "server", "operator_api", "runtime")
    names: set[str] = set()
    for root in roots:
        for path in (REPO / root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:  # a fixture of deliberately broken source
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    label = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                    if label != "tool":
                        continue
                    args = dec.args if isinstance(dec, ast.Call) else []
                    explicit = next(
                        (a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)), None
                    )
                    names.add(explicit or node.name)
    return names


def _backticked_identifiers() -> set[str]:
    out: set[str] = set()
    for path in sorted(SKILLS.rglob("SKILL.md")):
        text = path.read_text(encoding="utf-8")
        out |= set(re.findall(r"`([a-z_][a-z0-9_]*)`", text))
        out |= set(re.findall(r"`([a-z_][a-z0-9_]*)\(", text))
    return out


def test_every_tool_the_skills_name_exists_in_tree():
    """The whole point of the move: a skill that names a renamed or deleted core tool
    used to be undetectable — two repos, no shared test. Now it fails here."""
    in_tree = _in_tree_tool_names()
    assert "execute_code" in in_tree, "the tool sweep found nothing — has the @tool shape changed?"
    missing = sorted(SKILL_TOOL_CONTRACT - in_tree)
    assert not missing, f"cowork skills direct the agent to tools that no longer exist in-tree: {missing}"


def test_no_skill_backticks_an_unknown_tool_name():
    """Keeps the contract above from going stale: a NEW backticked name in a skill is
    either a real tool or a classified non-tool, never unreviewed."""
    unclassified = sorted(_backticked_identifiers() - NOT_TOOL_NAMES - _in_tree_tool_names())
    assert not unclassified, (
        f"backticked in a cowork skill but neither an in-tree tool nor a known non-tool: {unclassified} — "
        "add it to SKILL_TOOL_CONTRACT (a tool) or NOT_TOOL_NAMES (a param/library/config key)"
    )
    # Non-vacuous: the sweep really does reach the tool names.
    assert "execute_code" in _backticked_identifiers()


def test_the_watch_args_the_drop_folder_skill_names_are_real_create_watch_params():
    """drop-folder walks the operator through create_watch by parameter name."""
    source = (REPO / "tools" / "lg_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "create_watch"
    )
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert _WATCH_ARGS_THE_SKILLS_NAME <= params, (
        f"the skills name create_watch args that no longer exist: {sorted(_WATCH_ARGS_THE_SKILLS_NAME - params)}"
    )


# ── cowork:folder_changed — fence-honest, glob-aware, change-visible ───────────
#
# Ported from the standalone suite, which had to stub ``graph.goals.types``; in-tree the
# verifier resolves the REAL VerifyResult, so these now check the frozen contract
# (met, reason, evidence) against the host's own dataclass.


class _Ctx:
    def __init__(self, roots):
        self.config = types.SimpleNamespace(
            effective_filesystem_projects=lambda: [{"name": r.name, "path": str(r)} for r in roots]
        )


def _run(spec, ctx):
    return asyncio.run(_plugin()._folder_changed(spec, ctx))


def _spec(**args):
    return {"type": "plugin", "check": "cowork:folder_changed", "args": args}


@pytest.fixture
def fenced(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def test_the_fence_helpers_the_verifier_reads_exist_on_the_real_config():
    """``effective_filesystem_projects`` is the fence the verifier honours, with
    ``filesystem_projects`` as the older-host fallback. Both are read through
    getattr/try-except, so a rename in core would degrade this verifier to "no fenced
    work folders configured" — silently — on every poll."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert callable(cfg.effective_filesystem_projects)
    assert cfg.effective_filesystem_projects() == [] or isinstance(cfg.effective_filesystem_projects(), list)
    assert isinstance(cfg.filesystem_projects, list)


def test_the_verifier_returns_the_hosts_real_verify_result(fenced):
    from graph.goals.types import VerifyResult

    assert isinstance(_run(_spec(path=str(fenced)), _Ctx([fenced])), VerifyResult)


def test_met_when_files_match_and_evidence_lists_them(fenced):
    (fenced / "report.pdf").write_bytes(b"x")
    r = _run(_spec(path=str(fenced), glob="*.pdf"), _Ctx([fenced]))
    assert r.met and "report.pdf" in r.evidence and "1 file(s)" in r.reason


def test_not_met_when_nothing_matches_but_still_verifies(fenced):
    r = _run(_spec(path=str(fenced), glob="*.pdf"), _Ctx([fenced]))
    assert not r.met and r.evidence == ""


def test_evidence_moves_on_delete(fenced):
    a, b = fenced / "a.csv", fenced / "b.csv"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    ctx = _Ctx([fenced])
    before = _run(_spec(path=str(fenced), glob="*.csv"), ctx).evidence
    b.unlink()
    after = _run(_spec(path=str(fenced), glob="*.csv"), ctx).evidence
    assert before != after and "b.csv" not in after


def test_recursive_opt_in(fenced):
    sub = fenced / "inbox"
    sub.mkdir()
    (sub / "deep.txt").write_bytes(b"x")
    flat = _run(_spec(path=str(fenced), glob="*.txt"), _Ctx([fenced]))
    deep = _run(_spec(path=str(fenced), glob="*.txt", recursive=True), _Ctx([fenced]))
    assert not flat.met and deep.met


def test_subfolder_of_a_fenced_root_is_inside_the_fence(fenced):
    sub = fenced / "drops"
    sub.mkdir()
    (sub / "f.txt").write_bytes(b"x")
    assert _run(_spec(path=str(sub)), _Ctx([fenced])).met


def test_outside_the_fence_is_refused(tmp_path, fenced):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = _run(_spec(path=str(outside)), _Ctx([fenced]))
    assert not r.met and "outside the fenced" in r.reason


def test_no_fence_configured_is_refused(fenced):
    r = _run(_spec(path=str(fenced)), _Ctx([]))
    assert not r.met and "no fenced work folders" in r.reason


def test_missing_path_and_escaping_glob_are_refused(fenced):
    assert "args.path is required" in _run(_spec(), _Ctx([fenced])).reason
    r = _run(_spec(path=str(fenced), glob="../*"), _Ctx([fenced]))
    assert not r.met and "relative" in r.reason


def test_config_without_effective_helper_falls_back_to_raw_field(fenced):
    (fenced / "x.txt").write_bytes(b"x")
    ctx = types.SimpleNamespace(config=types.SimpleNamespace(filesystem_projects=[{"path": str(fenced)}]))
    assert _run(_spec(path=str(fenced)), ctx).met


# ── the eval suite stays runnable: valid JSON, known keys, one assertion/case ──
#
# The cases run against a live instance via protoAgent's own runner
# (``python -m evals.runner --tasks-file plugins/cowork/evals/tasks.json``); these
# checks only guard the file's shape, so a typo'd key fails here instead of silently
# asserting nothing there.

TASKS = ROOT / "evals" / "tasks.json"

# The runner ignores unknown keys, so a misspelled assertion key would pass
# vacuously — this vocabulary is the tripwire.
ALLOWED_KEYS = {
    "id",
    "category",
    "kind",
    "name",
    "prompt",
    "expected_tools",
    "expected_any_tools",
    "forbidden_tools",
    "expected_patterns",
    "forbidden_patterns",
    "tool_outcome",
    "verify_kb",
    "verify_rubric",
    "setup",
    "teardown",
    "requires_env",
}

ASSERTION_KEYS = {
    "expected_tools",
    "expected_any_tools",
    "forbidden_tools",
    "expected_patterns",
    "forbidden_patterns",
    "verify_kb",
    "verify_rubric",
}


def _cases() -> list[dict]:
    return json.loads(TASKS.read_text(encoding="utf-8"))


def test_tasks_parse_with_unique_prefixed_ids():
    cases = _cases()
    assert cases, "empty suite"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert all(i.startswith("cowork_") for i in ids), "ids must be cowork_-prefixed"


def test_every_case_is_an_ask_with_known_keys():
    for c in _cases():
        assert c["kind"] == "ask", c["id"]
        assert c.get("prompt", "").strip(), c["id"]
        assert c.get("category", "").startswith("cowork-"), c["id"]
        unknown = set(c) - ALLOWED_KEYS
        assert not unknown, f"{c['id']}: unknown keys {sorted(unknown)}"


def test_every_case_asserts_something():
    for c in _cases():
        assert ASSERTION_KEYS & set(c), f"{c['id']} asserts nothing"


def test_every_tool_the_eval_cases_assert_exists_in_tree():
    """The cases name tools too — same cross-repo drift, same guard."""
    in_tree = _in_tree_tool_names()
    named = {
        tool
        for c in _cases()
        for key in ("expected_tools", "expected_any_tools", "forbidden_tools")
        for tool in c.get(key, [])
    }
    assert named, "no case names a tool — this check would be vacuous"
    assert not (named - in_tree), f"eval cases assert tools that do not exist in-tree: {sorted(named - in_tree)}"
