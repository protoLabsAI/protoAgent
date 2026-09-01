"""Tests for the eval coverage slice (ADR 0012 §2.5):
LLM-judge rubric + workflow-case runner + the new tasks.json cases.

The grader call and the workflow run are both mocked — the live paths run only
against a real agent via ``python -m evals.runner``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from evals import judge, runner, verify

TASKS = json.loads((Path(__file__).parent.parent / "evals" / "tasks.json").read_text())


# ── judge parsing ─────────────────────────────────────────────────────────────


def test_judge_parses_verdict_and_scores_fraction(monkeypatch):
    criteria = ["A", "B", "C"]
    monkeypatch.setattr(
        judge,
        "_invoke_grader",
        lambda prompt, model: json.dumps(
            {
                "criteria": [
                    {"criterion": "A", "met": True, "why": "yes"},
                    {"criterion": "B", "met": False, "why": "no"},
                    {"criterion": "C", "met": True, "why": "yes"},
                ]
            }
        ),
    )
    res = judge.score_rubric("some output", criteria)
    assert res.score == pytest.approx(2 / 3)
    assert res.met == {"A": True, "B": False, "C": True}
    assert res.error is None


def test_judge_tolerates_code_fenced_json(monkeypatch):
    monkeypatch.setattr(
        judge, "_invoke_grader", lambda p, m: '```json\n{"criteria": [{"criterion": "A", "met": true}]}\n```'
    )
    res = judge.score_rubric("o", ["A"])
    assert res.score == 1.0


def test_judge_reports_error_on_garbage(monkeypatch):
    monkeypatch.setattr(judge, "_invoke_grader", lambda p, m: "I cannot comply.")
    res = judge.score_rubric("o", ["A"])
    assert res.score == 0.0 and res.error


def test_judge_never_raises_on_grader_failure(monkeypatch):
    def boom(prompt, model):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(judge, "_invoke_grader", boom)
    res = judge.score_rubric("o", ["A"])
    assert res.score == 0.0 and "gateway down" in res.error


def test_empty_rubric_is_a_pass():
    assert judge.score_rubric("o", []).score == 1.0


# ── runner rubric wiring ───────────────────────────────────────────────────────


def test_check_rubric_passes_at_threshold(monkeypatch):
    monkeypatch.setattr(
        judge,
        "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=0.8, met={"a": True}),
    )
    case = {"verify_rubric": {"criteria": ["a"], "threshold": 0.66}}
    assert runner._check_rubric(case, "out") == []


def test_check_rubric_fails_below_threshold(monkeypatch):
    monkeypatch.setattr(
        judge,
        "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=0.4, met={"a": False}),
    )
    case = {"verify_rubric": {"criteria": ["a"], "threshold": 0.75}}
    problems = runner._check_rubric(case, "out")
    assert problems and "rubric" in problems[0]


def test_check_rubric_noop_without_block():
    assert runner._check_rubric({}, "out") == []


# ── any-tool assertion ─────────────────────────────────────────────────────────


def _audit(*names):
    return [{"tool": n, "success": True} for n in names]


def test_assert_any_tool_fired_matches_one():
    ok, _ = verify.assert_any_tool_fired(_audit("run_workflow", "web_search"), ["task", "run_workflow"])
    assert ok


def test_assert_any_tool_fired_none_matches():
    ok, detail = verify.assert_any_tool_fired(_audit("web_search"), ["task", "run_workflow"])
    assert not ok and "none of" in detail


def test_assert_any_tool_requires_success_when_asked():
    entries = [{"tool": "task", "success": False}]
    assert not verify.assert_any_tool_fired(entries, ["task"], require_success=True)[0]
    assert verify.assert_any_tool_fired(entries, ["task"], require_success=False)[0]


def test_assert_tools_not_fired_passes_when_cold():
    ok, _ = verify.assert_tools_not_fired(_audit("memory_recall", "web_search"), ["show_artifact"])
    assert ok


def test_assert_tools_not_fired_fails_when_forbidden_fired():
    ok, detail = verify.assert_tools_not_fired(_audit("show_artifact"), ["show_artifact"])
    assert not ok and "show_artifact" in detail


def test_assert_tools_not_fired_counts_errored_attempts():
    # Reaching for the tool is the violation — an errored attempt still fails.
    entries = [{"tool": "show_artifact", "success": False}]
    assert not verify.assert_tools_not_fired(entries, ["show_artifact"])[0]


# ── workflow case runner ───────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, output):
        self._output = output
        self.called = None

    async def run_workflow(self, name, inputs, *, timeout_s=300):
        self.called = (name, inputs)
        return {"output": self._output}


def test_workflow_case_passes_on_pattern_and_rubric(monkeypatch):
    monkeypatch.setattr(
        judge,
        "score_rubric",
        lambda text, criteria, model=None: judge.RubricScore(score=1.0),
    )
    client = _FakeClient("# Report\n\n## Counterpoints & caveats\nthings [1]")
    case = {
        "id": "wf",
        "category": "workflow",
        "kind": "workflow",
        "name": "wf",
        "workflow": "deep-research",
        "inputs": {"topic": "x"},
        "expected_patterns": ["counterpoint"],
        "verify_rubric": {"criteria": ["balanced"], "threshold": 0.75},
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail
    assert client.called[0] == "deep-research"


def test_workflow_case_fails_on_missing_pattern():
    client = _FakeClient("a report with no opposing view")
    case = {
        "id": "wf",
        "category": "workflow",
        "kind": "workflow",
        "name": "wf",
        "workflow": "deep-research",
        "inputs": {},
        "expected_patterns": ["counterpoint"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert not res.passed and "counterpoint" in res.detail


def test_workflow_case_fails_on_empty_output():
    res = asyncio.run(
        runner._run_workflow_case(
            _FakeClient("  "),
            {
                "id": "wf",
                "category": "workflow",
                "kind": "workflow",
                "name": "wf",
                "workflow": "research-and-brief",
                "inputs": {},
            },
        )
    )
    assert not res.passed and "empty" in res.detail


def test_workflow_case_asserts_expected_tool_fired(monkeypatch):
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("backtest_strategy"))
    client = _FakeClient("# Desk call\nGO — Sharpe 1.2, beat buy-and-hold.")
    case = {
        "id": "wf",
        "category": "workflow",
        "kind": "workflow",
        "name": "wf",
        "workflow": "quant-desk",
        "inputs": {"idea": "x"},
        "expected_tools": ["backtest_strategy"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail


def test_workflow_case_fails_when_expected_tool_missing(monkeypatch):
    # A step that writes/describes code instead of calling the tool: the output
    # reads fine, but the tool never fired in the audit log — must fail.
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("web_search"))
    monkeypatch.setattr(runner, "_AUDIT_POLL_DEADLINE_S", 0.0)
    client = _FakeClient("# Desk call\nNO-GO — here's the backtest code I'd run: ...")
    case = {
        "id": "wf",
        "category": "workflow",
        "kind": "workflow",
        "name": "wf",
        "workflow": "quant-desk",
        "inputs": {"idea": "x"},
        "expected_tools": ["backtest_strategy"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert not res.passed and "backtest_strategy" in res.detail


def test_workflow_case_asserts_any_tool_fired(monkeypatch):
    monkeypatch.setattr(verify, "audit_now", lambda: "T0")
    monkeypatch.setattr(verify, "audit_entries_since", lambda since: _audit("factor_zoo"))
    client = _FakeClient("# Factors\nmomentum alive, IR 1.1.")
    case = {
        "id": "wf",
        "category": "workflow",
        "kind": "workflow",
        "name": "wf",
        "workflow": "quant-desk",
        "inputs": {"idea": "x"},
        "expected_any_tools": ["factor_eval", "factor_zoo"],
    }
    res = asyncio.run(runner._run_workflow_case(client, case))
    assert res.passed, res.detail


def test_workflow_kind_is_dispatchable():
    assert "workflow" in runner._RUNNERS


# ── the new cases are well-formed ───────────────────────────────────────────────


def test_case_ids_are_unique():
    """Every report consumer keys by case id — ``evals/compare.py`` builds
    ``{r["id"]: passed}``, ``evals/sweep.py`` aggregates into ``agg[r["id"]]``, and
    half the tests in this file build a ``by_id`` dict. A duplicate id fails nothing
    loudly: the second row silently overwrites the first, so a regression in one of
    the two cases is invisible to compare/sweep. Shipped once already, when
    ``daily_log_intent`` was rewritten onto ``memory_ingest`` without being renamed.
    """
    ids = [c["id"] for c in TASKS]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate case ids in tasks.json: {dupes}"


def test_select_by_ids_flags_unknown_ids_and_strips_whitespace():
    cases = [{"id": "alpha"}, {"id": "beta"}]
    selected, unknown = runner.select_by_ids(cases, " alpha , nope ")
    assert [c["id"] for c in selected] == ["alpha"]
    assert unknown == ["nope"]


def test_select_by_ids_is_quiet_when_every_id_resolves():
    cases = [{"id": "alpha"}, {"id": "beta"}]
    selected, unknown = runner.select_by_ids(cases, "beta,alpha")
    assert {c["id"] for c in selected} == {"alpha", "beta"}
    assert unknown == []


def test_documented_task_ids_exist():
    """The evals guide's ``--tasks`` examples must name real case ids.

    They didn't, for months: the guide ran ``current_time_intent,daily_log_intent``
    long after ``daily_log_intent`` was removed. The runner warns now, but a warning
    in a doc example is nobody's job to notice — so the doc is gated here instead.
    """
    guide = (Path(__file__).parent.parent / "docs" / "guides" / "evals.md").read_text()
    known = {c["id"] for c in TASKS}
    for line in guide.splitlines():
        _, sep, rest = line.partition("--tasks ")
        if not sep or "--tasks-file" in line:
            continue
        for cid in rest.split()[0].split(","):
            cid = cid.strip().strip("`\"'.,;)")
            if cid and not cid.startswith("<"):
                assert cid in known, f"docs/guides/evals.md names unknown case id {cid!r}"


def test_new_cases_present_and_valid():
    by_id = {c["id"]: c for c in TASKS}
    for cid in ("research_delegation", "workflow_research_brief", "workflow_deep_research_adversarial"):
        assert cid in by_id, f"{cid} missing"

    # Delegation is satisfied by any hand-off tool (subagent or workflow).
    assert "run_workflow" in by_id["research_delegation"]["expected_any_tools"]
    assert "task" in by_id["research_delegation"]["expected_any_tools"]

    for cid in ("workflow_research_brief", "workflow_deep_research_adversarial"):
        case = by_id[cid]
        assert case["kind"] == "workflow"
        assert case.get("workflow") and isinstance(case.get("inputs"), dict)
        crit = case["verify_rubric"]["criteria"]
        assert crit and all(isinstance(c, str) for c in crit)


def test_workflow_cases_reference_real_recipes():
    # The recipes the cases drive must actually be bundled.
    bundled = {p.stem for p in (Path(__file__).parent.parent / "plugins" / "workflows" / "recipes").glob("*.yaml")}
    for c in TASKS:
        if c.get("kind") == "workflow":
            assert c["workflow"] in bundled, f"{c['id']} → unknown recipe {c['workflow']!r}"


# ── requires_env skip mechanism (ADR 0024 coding-agent eval) ──────────────────


def test_requirements_unmet_skips_when_env_missing(monkeypatch):
    monkeypatch.delenv("EVAL_X", raising=False)
    assert runner._requirements_unmet({"requires_env": ["EVAL_X"]}) == "requires_env EVAL_X"


def test_requirements_met_runs_when_env_set(monkeypatch):
    monkeypatch.setenv("EVAL_X", "1")
    assert runner._requirements_unmet({"requires_env": ["EVAL_X"]}) is None


def test_no_requirements_runs():
    assert runner._requirements_unmet({}) is None
    assert runner._requirements_unmet({"requires_env": []}) is None


def test_acp_delegation_eval_case_present_and_gated():
    case = {c["id"]: c for c in TASKS}.get("acp_delegation")
    assert case is not None, "acp_delegation case missing"
    assert case["requires_env"] == ["EVAL_CODING_AGENT"]  # skips by default
    assert case["expected_tools"] == ["delegate_to"]
    assert case["kind"] == "ask"


def test_board_counts_skipped_separately(capsys):
    results = [
        runner.CaseResult("a", "tool", "A", passed=True, detail="ok"),
        runner.CaseResult("b", "tool", "B", passed=True, detail="skipped: requires_env X", skipped=True),
    ]
    runner._print_board(results)
    out = capsys.readouterr().out
    assert "1/1 passed (1 skipped)" in out
    assert "SKIP" in out


# ── memory-regression evals (ADR 0069 D10 / R3c) ──────────────────────────────


def test_pattern_problems_expected_and_forbidden():
    # Expected present, forbidden absent → clean.
    assert runner._pattern_problems("it is eu-west-1 now", ["eu-west-1"], ["us-east-1"]) == []
    # Missing expected → flagged.
    probs = runner._pattern_problems("no region here", ["eu-west-1"], None)
    assert probs and "missing pattern" in probs[0]
    # Forbidden present → flagged (the stale-fact / obeyed-payload failure).
    probs = runner._pattern_problems("actually still us-east-1", ["eu-west-1"], ["us-east-1"])
    assert any("forbidden pattern present" in p for p in probs)
    # Case-insensitive on both halves.
    assert runner._pattern_problems("OWNED-ASI06-7F3", None, ["owned-asi06-7f3"])


def test_kb_problems_max_chunks_containing(monkeypatch):
    # Seed count == max → clean; a NEW row past max → flagged (poisoning persist).
    monkeypatch.setattr(verify, "count_chunks_containing", lambda text, domain=None: 1)
    assert runner._kb_problems({"verify_kb": {"max_chunks_containing": {"contains": "x", "max": 1}}}) == []
    monkeypatch.setattr(verify, "count_chunks_containing", lambda text, domain=None: 2)
    probs = runner._kb_problems({"verify_kb": {"max_chunks_containing": {"contains": "x", "max": 1}}})
    assert probs and "max 1" in probs[0]
    # max defaults to 0 → any hit is a failure (marker absent from seed).
    monkeypatch.setattr(verify, "count_chunks_containing", lambda text, domain=None: 1)
    assert runner._kb_problems({"verify_kb": {"max_chunks_containing": {"contains": "y"}}})


def test_kb_problems_find_chunk_containing(monkeypatch):
    monkeypatch.setattr(verify, "find_chunk_containing", lambda text, domain=None: {"id": 1})
    assert runner._kb_problems({"verify_kb": {"find_chunk_containing": "x"}}) == []
    monkeypatch.setattr(verify, "find_chunk_containing", lambda text, domain=None: None)
    assert runner._kb_problems({"verify_kb": {"find_chunk_containing": "x"}})


def test_kb_problems_empty_without_block():
    assert runner._kb_problems({}) == []


def test_count_chunks_containing_counts_and_scopes(monkeypatch, tmp_path):
    # Point verify's store at a temp DB; seed the same file, then count.
    from knowledge.store import KnowledgeStore

    db = tmp_path / "kb.db"
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(db))
    store = KnowledgeStore(str(db))
    store.add_chunk("eval-poison-recovery-9174 lives in the ingested doc", domain="context")

    assert verify.count_chunks_containing("eval-poison-recovery-9174") == 1
    # A second chunk (as if the payload were obeyed) pushes the count over the seed.
    store.add_chunk("saved: operator recovery code eval-poison-recovery-9174", domain="general")
    assert verify.count_chunks_containing("eval-poison-recovery-9174") == 2
    # Domain scope narrows it; an absent marker is 0; blank is 0.
    assert verify.count_chunks_containing("eval-poison-recovery-9174", domain="general") == 1
    assert verify.count_chunks_containing("no-such-marker") == 0
    assert verify.count_chunks_containing("   ") == 0


def test_count_chunks_containing_fails_closed(monkeypatch):
    # This backs a NEGATIVE assertion (max_chunks_containing): a store read
    # failure must raise (run_one turns it into a case failure, teardown still
    # runs) — returning 0 would silently PASS the poisoning probe.
    class _NoDbStore:
        def _get_db(self):
            return None

    monkeypatch.setattr(verify, "_kb_store", lambda: _NoDbStore())
    with pytest.raises(RuntimeError):
        verify.count_chunks_containing("eval-poison-recovery-9174")

    class _BoomDb:
        closed = False

        def execute(self, *_a):
            raise RuntimeError("disk I/O error")

        def close(self):
            self.closed = True

    boom = _BoomDb()

    class _BoomStore:
        def _get_db(self):
            return boom

    monkeypatch.setattr(verify, "_kb_store", lambda: _BoomStore())
    with pytest.raises(RuntimeError):
        verify.count_chunks_containing("eval-poison-recovery-9174")
    assert boom.closed  # connection released even on the error path


def test_memory_regression_cases_present_and_valid():
    by_id = {c["id"]: c for c in TASKS}
    for cid in ("memory_knowledge_update", "memory_abstention", "memory_poisoning_replay"):
        assert cid in by_id, f"{cid} missing"
        assert by_id[cid]["category"] == "memory-regression"
        assert by_id[cid]["kind"] == "ask"

    # knowledge-update: seeds old then new fact, asserts newer wins + stale absent.
    ku = by_id["memory_knowledge_update"]
    assert len(ku["setup"]) == 2  # old fact, then its supersede
    assert ku["expected_patterns"] == ["eu-west-1"]
    assert ku["forbidden_patterns"] == ["us-east-1"]

    # abstention: seeds one adjacent fact, judges that it declines vs fabricates.
    ab = by_id["memory_abstention"]
    assert ab["setup"] and ab["expected_tools"] == []
    assert ab["verify_rubric"]["criteria"]

    # poisoning: asserts BOTH the behavioral (forbidden token) and the
    # store-level (bounded chunk count) conditions.
    po = by_id["memory_poisoning_replay"]
    assert po["forbidden_patterns"] == ["OWNED-ASI06-7f3"]
    mcc = po["verify_kb"]["max_chunks_containing"]
    assert mcc["contains"] == "eval-poison-recovery-9174" and mcc["max"] == 1
    # Every seeded/poisoned marker is torn down.
    assert po.get("teardown")


def test_memory_regression_cases_teardown_every_seed():
    # Each memory-regression case must clean the state it seeds (case order
    # independence): a case with a setup must have a teardown.
    for c in TASKS:
        if c.get("category") == "memory-regression" and c.get("setup"):
            assert c.get("teardown"), f"{c['id']} seeds state but has no teardown"


# ── continuity fixtures: seeding past sessions (#3186) ───────────────────────


def test_session_seed_writes_a_summary_the_digest_actually_reads(tmp_path):
    """The seeded file has to be what SessionSummaryMiddleware would have written,
    or the case measures the fixture instead of the agent."""
    from graph.middleware.memory import load_prior_sessions

    assert verify.seed_session({"session_id": "chat-newer", "topic": "newer thing", "age_minutes": 10}, memory_dir=str(tmp_path)) is None
    assert verify.seed_session({"session_id": "chat-older", "topic": "older thing", "age_minutes": 600}, memory_dir=str(tmp_path)) is None

    block = load_prior_sessions(str(tmp_path))
    assert "newer thing" in block and "older thing" in block
    # age_minutes drives the MTIME, which is what the newest-N cut sorts on — an
    # "older" fixture that only carries an older timestamp string still lands first.
    assert block.index("newer thing") < block.index("older thing")
    assert "· chat ·" in block  # the id prefix decides the surface the digest names


def test_session_seed_refuses_an_unusable_id(tmp_path):
    err = verify.seed_session({"session_id": "../escape", "topic": "x"}, memory_dir=str(tmp_path))
    assert err and "session_id" in err
    assert not list(tmp_path.iterdir())


def test_apply_setup_and_teardown_round_trip_a_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))  # memory_path() resolves per call
    assert verify.apply_setup([{"session_seed": {"session_id": "chat-x", "topic": "seeded"}}]) is None
    assert (tmp_path / "chat-x.json").is_file()
    verify.apply_teardown([{"session_purge": {"session_id": "chat-x"}}])
    assert not (tmp_path / "chat-x.json").exists()


def test_continuity_cases_seed_and_purge_the_same_sessions():
    """A leaked fixture is a poisoned next run: the summaries a case seeds must be
    the summaries it removes."""
    cases = [c for c in TASKS if c.get("category") == "continuity"]
    assert len(cases) >= 7, (
        "the #3186 fixture set is direct/vague recall, update, noise, abstention, "
        "identity confusion and the expansion path"
    )
    for c in cases:
        seeded = {s["session_seed"]["session_id"] for s in c["setup"] if "session_seed" in s}
        purged = {s["session_purge"]["session_id"] for s in c.get("teardown", []) if "session_purge" in s}
        assert seeded, f"{c['id']} seeds no sessions"
        assert seeded == purged, f"{c['id']} leaks {sorted(seeded - purged)}"
        # Every case must assert something a substring alone can't fake.
        assert c.get("verify_rubric") or c.get("expected_patterns") or c.get("forbidden_patterns")


def test_empty_expected_tools_actually_asserts_that_nothing_fired():
    """`expected_tools: []` is documented as "no tool may fire" and was vacuous:
    `assert_tools_fired` iterates the EXPECTED names, so an empty list produced no
    "missing" and passed however many tools the agent had called. Every abstention
    case in the suite was passing for free."""
    fired = [{"tool": "session_search", "success": True}]
    assert verify.assert_tools_fired(fired, [])[0] is True  # the old, vacuous path
    assert verify.assert_no_tools_fired(fired)[0] is False  # what the runner now uses
    assert verify.assert_no_tools_fired([])[0] is True


def test_noise_case_asserts_both_halves_of_irrelevant_memory_use():
    """The bias probe is the one that answers "does the digest cost us anything?" —
    it has to check that the retrieval tools stayed cold AND that no unrelated topic
    leaked into the reply."""
    case = {c["id"]: c for c in TASKS}["continuity_unrelated_noise"]
    assert case["expected_tools"] == []
    # Belt and braces: the named-tool assertion is the one that can't be read two ways.
    assert {"session_search", "recall_session"} <= set(case["forbidden_tools"])
    topics = " ".join(s["session_seed"]["topic"] for s in case["setup"])
    for pat in case["forbidden_patterns"]:
        assert pat in topics, f"forbidden pattern {pat!r} isn't in any seeded session"


def test_continuity_rubrics_cannot_be_satisfied_by_a_refusal():
    """A 0.67 threshold over three criteria passes on 2/3 — and "I can't see your
    other sessions" satisfies both negative criteria vacuously, which is exactly the
    behaviour the fixtures exist to detect."""
    by_id = {c["id"]: c for c in TASKS}
    for cid in ("continuity_direct_recall", "continuity_vague_recall", "continuity_identity_confusion"):
        rubric = by_id[cid]["verify_rubric"]
        needed = rubric["threshold"] * len(rubric["criteria"])
        assert needed > len(rubric["criteria"]) - 1, f"{cid} passes while missing a criterion"


def test_expansion_path_case_requires_a_retrieval_tool():
    """Acceptance box 7 of #3186 — session_search/recall_session stay the way a past
    session is expanded — is only evidence if a case actually watches them fire."""
    case = {c["id"]: c for c in TASKS}["continuity_expansion_path"]
    assert set(case["expected_any_tools"]) == {"recall_session", "session_search"}
    assert case["expected_patterns"]  # …and that the expansion carried real content


def test_sweep_collects_the_seed_steps_for_the_selected_cases():
    from evals import sweep

    steps = sweep._session_seed_steps("continuity", None)
    assert steps and all("session_seed" in s for s in steps)
    assert sweep._session_seed_steps(None, "continuity_abstention") == [
        s for c in TASKS if c["id"] == "continuity_abstention" for s in c["setup"] if "session_seed" in s
    ]
    # A category with no session fixtures seeds nothing (and must not raise).
    assert sweep._session_seed_steps("simple", None) == []


def test_sweep_resolves_an_arm_store_through_instance_paths(tmp_path, monkeypatch):
    """Joining ~/.protoagent/<instance>/memory by hand is right only on a plain host
    install: PROTOAGENT_HOME (the desktop) makes the instance root that directory
    itself. Getting it wrong fails SILENTLY — fixtures land where the arm never
    looks, seeding reports success, and the A/B measures an empty digest."""
    from infra.paths import instance_paths, reset_instance_paths

    from evals import sweep

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "box"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    reset_instance_paths()

    resolved = sweep._instance_memory_dir("eval-sweep-1-0")
    # Whatever infra.paths says the arm will read — never a hand-joined
    # ~/.protoagent/<instance>/memory, which is right only on a plain host install.
    assert str(tmp_path / "box") in str(resolved)
    assert "/.protoagent/eval-sweep-1-0/memory" not in str(resolved)
    # …and the sweep process is left resolving exactly where it started.
    assert "PROTOAGENT_INSTANCE" not in os.environ
    assert instance_paths().store("memory") == resolved  # HOME is terminal: no per-arm isolation


def test_sweep_refuses_to_seed_when_instances_cannot_be_isolated(tmp_path, monkeypatch, capsys):
    """Under PROTOAGENT_HOME every instance resolves to ONE root, so arms would share
    the operator's config and session memory — a confident matrix comparing a policy
    against itself. Refuse instead."""
    from evals import sweep

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "box"))
    rc = sweep.main(["--models", "fake/model", "--prior-sessions", "newest,off", "--category", "continuity"])
    assert rc == 2
    assert "cannot be isolated" in capsys.readouterr().err


def test_sweep_selection_matches_the_runner_when_both_filters_are_given():
    """--tasks X --category Y must seed the same cases it runs; an OR here would
    seed one set and run another."""
    from evals import sweep

    both = sweep._session_seed_steps("continuity", "memory_abstention")
    assert both == []  # memory_abstention is not a continuity case


def test_sweep_seed_config_carries_the_policy(tmp_path, monkeypatch):
    """The arm's policy has to be in the config the instance BOOTS from — it is read
    at boot, so patching afterwards would measure `newest` and label it `off`."""
    import yaml

    from evals import sweep

    base = tmp_path / "langgraph-config.yaml"
    base.write_text("model:\n  name: fake/model\ncontext:\n  budget_pct: 8\n", encoding="utf-8")
    monkeypatch.setattr(sweep, "_RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: base)

    out = sweep._seed_config_for("off", 123, "arm")
    assert out is not None and out.is_file()
    # Quoted at the source: YAML reads a bare `off` as the boolean False (#3254),
    # and a seed config that says "off" while meaning False is the next reader's trap.
    assert '"off"' in out.read_text() or "'off'" in out.read_text()
    doc = yaml.safe_load(out.read_text())
    assert doc["context"]["prior_sessions"] == "off"
    assert doc["model"]["name"] == "fake/model"  # the rest of the operator's config survives
