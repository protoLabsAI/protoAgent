"""Offline tests for the on-demand memory measurement runner."""

import hashlib
import json
from collections import Counter
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from evals import memory_retrieval as ev
from graph.projection import ProjectionOptions
from knowledge.memories import MemoryStore


@pytest.mark.parametrize("raises", [False, True])
def test_topk_restores_state(monkeypatch, raises):
    from graph import projection
    from runtime.state import STATE

    original, fixture = object(), object()
    monkeypatch.setattr(STATE, "memory_store", original)
    opts = ProjectionOptions()

    def retrieve(query, options):
        assert query == "weekend"
        assert options is opts
        assert STATE.memory_store is fixture
        if raises:
            raise RuntimeError("retrieval failure")
        return [{"content": "hiking"}]

    monkeypatch.setattr(projection, "_relevant_memory", retrieve)
    if raises:
        with pytest.raises(RuntimeError, match="retrieval failure"):
            ev.topk("weekend", fixture, opts)
    else:
        assert ev.topk("weekend", fixture, opts) == [{"content": "hiking"}]
    assert STATE.memory_store is original


@pytest.mark.parametrize("vector,passed", [([2, 0], True), ([0, 2], False)])
def test_pv4_nearest_nonempty_choice(vector, passed):
    case = dict(id="pv4-test", question="Where do I live?", answer="Tucson",
                choices=["", "Portland", "Tucson", "  "])
    model = Mock()
    model.invoke.return_value = AIMessage(content="generated answer")
    embed = Mock(side_effect=lambda text: {"generated answer": vector,
                                         "Tucson": [10, 0], "Portland": [0, 1]}[text])
    result = ev.score_pv4(case, [{"content": "I live in Tucson."}], model, embed)
    assert result["passed"] is passed
    model.invoke.assert_called_once()
    messages = model.invoke.call_args.args[0]
    assert messages[0].content == ev.SYSTEM_PROMPT
    assert "I live in Tucson." in messages[1].content
    assert case["question"] in messages[1].content
    assert embed.call_count == 3


@pytest.mark.parametrize("contents,passed", [([], False), (["old"], False),
                                            (["new", "old"], False), (["new"], True)])
def test_changed_fact_scoring(contents, passed):
    case = dict(must_retrieve="new", must_not_retrieve="old")
    assert ev.score_changed_fact(case, [{"content": c} for c in contents])["passed"] is passed


@pytest.fixture
def fixture_file(tmp_path):
    from evals.memory_fixtures import FIELDS
    from contextlib import closing

    store = MemoryStore(tmp_path / "source.db")
    store.add("My favourite museum has dinosaur skeletons.", kind="fact", provenance="operator")
    with closing(store._connect()) as db:
        rows = [dict(row) for row in db.execute(f"SELECT {', '.join(FIELDS)} FROM memories")]
    path = tmp_path / "user_1.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.mark.platform_sensitive
def test_twenty_cases_consolidate_on_copies(fixture_file):
    from contextlib import closing

    cases = ev.load_cases(ev.FIXTURES / "supersede_cases.jsonl")
    assert len(cases) == len({case["id"] for case in cases}) == 20
    before = fixture_file.read_bytes()
    for case in cases:
        assert case["subject"].casefold() not in case["question"].casefold()
        store = ev.seed_changed_fact(case, fixture_file, None)
        with closing(store._connect()) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM memories ORDER BY id")]
        assert len(rows) == 3  # distractor plus both chronological facts
        assert rows[1]["superseded_by"] == rows[2]["id"]
        assert rows[2]["superseded_by"] is None
        # Real projection excludes the superseded row, even if both share query terms.
        retrieved = ev.topk(case["memories"][-1], store, ProjectionOptions(top_k=10))
        assert ev.score_changed_fact(case, retrieved)["passed"]
    assert fixture_file.read_bytes() == before


def test_paired_majorities():
    rows = []
    for case, (a, b) in enumerate([(2, 1), (1, 2), (3, 2), (0, 1)]):
        for arm, passes in [("a", a), ("b", b)]:
            rows.extend(dict(case_id=str(case), arm=arm, run=i, passed=i <= passes) for i in range(1, 4))
    assert ev.paired_table(rows, ["a", "b"], 3) == [
        dict(a="a", b="b", a_wins=1, b_wins=1, ties=2)]
    with pytest.raises(ValueError, match="requires all"):
        ev.paired_table(rows[:-1], ["a", "b"], 3)


@pytest.mark.platform_sensitive
def test_evaluate_mixed_suites(fixture_file, monkeypatch):
    case = dict(id="pv4-test", user_id=1, question="Where?", answer="here", choices=["here", "there"])
    changed = ev.load_cases(ev.FIXTURES / "supersede_cases.jsonl")[:1]
    arm = Mock(return_value=[{"content": changed[0]["memories"][-1]}])
    monkeypatch.setitem(ev.ARMS, "fake", arm)
    monkeypatch.setitem(ev.ARMS, "other", arm)
    model = Mock()
    model.invoke.return_value = AIMessage(content="here")
    report = ev.evaluate([case], changed, fixture_file.parent, ["fake", "other"], 3,
                         ProjectionOptions(), model, lambda s: [1, 0] if s == "here" else [0, 1])
    assert len(report["results"]) == arm.call_count == 12
    assert model.invoke.call_count == 6
    stores = [call.args[1] for call in arm.call_args_list]
    assert len({id(store) for store in stores}) == 12
    assert len({store.path for store in stores}) == 12
    assert report["paired"] == [dict(a="fake", b="other", a_wins=0, b_wins=0, ties=2)]
    assert all(row["passed"] for row in report["results"])


def _hash_embed(text):
    """Deterministic, content-addressed vector so repeats are identical."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [float(b) for b in digest[:8]]


def _mixed_suite_args(fixture_file):
    """Shared setup: one PV4 case plus one changed-fact case over the tiny fixture."""
    pv4 = [dict(id="pv4-memo", user_id=1, question="Where?", answer="here", choices=["here", "there"])]
    changed = ev.load_cases(ev.FIXTURES / "supersede_cases.jsonl")[:1]
    arm = Mock(return_value=[{"content": changed[0]["memories"][-1]}])
    model = Mock()
    model.invoke.return_value = AIMessage(content="here")
    return pv4, changed, arm, model


def test_memo_embed_copies_out_and_passes_none_through():
    calls = []

    def embed(text):
        calls.append(text)
        return None if text == "skip" else [1.0, float(len(calls))]

    memo = ev._memo_embed(embed)
    first = memo("fact")
    first.append(99)  # mutating a returned vector must not corrupt the cache
    assert memo("fact") == [1.0, 1.0]
    assert memo("skip") is None and memo("skip") is None  # None is never cached
    assert calls == ["fact", "skip", "skip"]  # "fact" embedded once, "skip" every time


@pytest.mark.platform_sensitive
def test_evaluate_memoizes_embeds_across_runs(fixture_file, monkeypatch):
    pv4, changed, arm, model = _mixed_suite_args(fixture_file)
    monkeypatch.setitem(ev.ARMS, "fake", arm)
    monkeypatch.setitem(ev.ARMS, "other", arm)
    calls = []

    def embed(text):
        calls.append(text)
        return _hash_embed(text)

    report = ev.evaluate(pv4, changed, fixture_file.parent, ["fake", "other"], 3,
                         ProjectionOptions(), model, embed)
    # runs=3 x 2 arms would re-embed every fixture row and choice many times;
    # memoization collapses each distinct text to exactly one underlying call.
    assert calls, "expected the fake embedder to be exercised"
    assert all(count == 1 for count in Counter(calls).values())
    assert len(report["results"]) == 12


@pytest.mark.platform_sensitive
def test_evaluate_pass_counts_match_unmemoized(fixture_file, monkeypatch):
    pv4, changed, arm, model = _mixed_suite_args(fixture_file)
    monkeypatch.setitem(ev.ARMS, "fake", arm)
    monkeypatch.setitem(ev.ARMS, "other", arm)

    def run():
        return ev.evaluate(pv4, changed, fixture_file.parent, ["fake", "other"], 3,
                           ProjectionOptions(), model, _hash_embed)

    def passes(report):
        return Counter((row["case_id"], row["arm"]) for row in report["results"] if row["passed"])

    memoized = run()
    # Identity wrapper reproduces the pre-change (unmemoized) embed path.
    monkeypatch.setattr(ev, "_memo_embed", lambda fn: fn)
    unmemoized = run()
    assert passes(memoized) == passes(unmemoized)
    assert memoized["paired"] == unmemoized["paired"]


@pytest.mark.platform_sensitive
def test_on_demand_only():
    root = ev.FIXTURES.parent.parent
    for relative in ("evals/tasks.json", "scripts/evals_nightly.py"):
        assert "memory_retrieval" not in (root / relative).read_text(encoding="utf-8")
    readme = (root / "evals/README.md").read_text(encoding="utf-8")
    assert "## Memory retrieval (on demand)" in readme
    assert "not part of the nightly gate" in readme


@pytest.mark.platform_sensitive
def test_cli_writes_report(fixture_file, monkeypatch, tmp_path):
    from graph.config import LangGraphConfig
    from graph import llm

    fixtures = tmp_path / "fixtures"
    (fixtures / "pv4").mkdir(parents=True)
    (fixtures / "pv4/user_1.jsonl").write_bytes(fixture_file.read_bytes())
    case = dict(id="pv4-cli", user_id=1, question="What do I enjoy?", answer="musées", choices=["musées", ""])
    (fixtures / "pv4/cases.jsonl").write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    (fixtures / "supersede_cases.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(ev, "FIXTURES", fixtures)
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(LangGraphConfig, "from_yaml", lambda _: LangGraphConfig())
    model = Mock()
    model.invoke.return_value = AIMessage(content="musées")
    factory = Mock(return_value=model)
    monkeypatch.setattr(llm, "create_llm", factory)
    monkeypatch.setattr(llm, "create_embed_fn", lambda _: lambda text: [1, 0])
    monkeypatch.setitem(ev.ARMS, "fake", lambda query, store, opts: [{"preview": "museum"}])
    assert ev.main(["--arms", "fake", "--model", "test-model", "--json", "out.json"]) == 0
    report = json.loads((tmp_path / "results/out.json").read_text(encoding="utf-8"))
    assert report["model"] == "test-model"
    assert report["results"][0]["response"] == "musées"
    assert len(report["results"]) == 3
    assert all(row["passed"] for row in report["results"])
    assert factory.call_args.kwargs["model_name"] == "test-model"
