"""Bounded, policy-driven delivery (ADR 0108 D6, #3187).

The projected context has a char budget (``context.budget_pct`` of the model
window, chars//4). Fill priority: working state → always-on memory → skill
index → prior-session digest → RAG hits. Over budget, the lowest-priority parts
shed first — RAG hits one by one from the lowest-ranked end, then the digest,
then skill descriptions down to the identity floor; working state and always-on
memory are never shed. Always-on is a delivery POLICY (``delivery_policy=
"always"``), not the ``hot`` domain; rejected and expired rows never deliver.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from graph.projection import (
    ProjectedContext,
    ProjectionOptions,
    compose_projected_context,
    deliverable_hit,
)
from knowledge.hybrid_store import HybridKnowledgeStore
from knowledge.layered import LayeredKnowledgeStore
from knowledge.store import KnowledgeStore


# ── fakes ────────────────────────────────────────────────────────────────────


class _Store:
    def __init__(self, *, hot=(), results=(), reject_deliverable_kwarg=False):
        self.hot = list(hot)
        self.results = list(results)
        self.reject_deliverable_kwarg = reject_deliverable_kwarg
        self.search_kwargs: list[dict] = []

    def get_hot_memory_entries(self, max_chars=6000):
        return list(self.hot)

    def get_hot_memory(self, max_chars=6000):
        return "\n".join(p for _, p in self.hot)

    def search(self, query, k=5, **kwargs):
        if self.reject_deliverable_kwarg and "deliverable" in kwargs:
            raise TypeError("unexpected keyword argument 'deliverable'")
        self.search_kwargs.append({"k": k, **kwargs})
        return list(self.results)


class _Index:
    def __init__(self, rows):
        self.rows = list(rows)

    def skill_summaries(self, limit=None):
        return list(self.rows)

    def discoverable_count(self):
        return len(self.rows)


def _hits(n: int, size: int = 80) -> list[dict]:
    return [
        {
            "id": i,
            "preview": f"hit-{i} " + ("x" * size),
            "domain": "general",
            "source_type": "operator",
            "created_at": "2026-08-01T00:00:00+00:00",
        }
        for i in range(1, n + 1)
    ]


_DIGEST = "<prior_sessions>\n- s1 · 2026-08-20 · release prep\n- s2 · 2026-08-19 · notes\n</prior_sessions>"
_DIGEST_IDS = ["s1", "s2"]
_HOT = [(7, "[ops] deploys go out Fridays"), (9, "operator prefers dark mode")]
_SKILLS = [
    {"name": f"skill-{i}", "description": "a fairly long description of what this skill does " * 3, "slash": ""}
    for i in range(6)
]
_WS = "<working_state>\nGOAL [active] (iteration 1/3): ship it\n</working_state>"


@pytest.fixture
def quiet(monkeypatch):
    """No working state, no goal turn, no disk, fresh once-per-process flags:
    every test states what it wants."""
    import graph.projection as proj

    monkeypatch.setattr(proj, "working_state_block", lambda state: "")
    monkeypatch.setattr(proj, "_in_goal_turn", lambda: False)
    proj._NEVER_SHED_WARNED.clear()
    monkeypatch.setattr(proj, "_INERT_BUDGET_LOGGED", False)


def _full_rows(text: str) -> int:
    """Skill rows carrying a description end with ``</skill>``; name-only rows are self-closing."""
    return text.count("</skill>")


def _compose(store, index, *, budget, digest=True, ws=None, monkeypatch=None, top_k=5):
    if ws is not None and monkeypatch is not None:
        import graph.projection as proj

        monkeypatch.setattr(proj, "working_state_block", lambda state: ws)
    return compose_projected_context(
        "what is the deploy day?",
        store,
        index,
        {"session_id": "s"},
        record=False,
        options=ProjectionOptions(top_k=top_k, skills_index_chars=8192, budget_chars=budget),
        prior_sessions=(lambda: (_DIGEST, list(_DIGEST_IDS))) if digest else (lambda: ("", [])),
    )


# ── budget derivation ────────────────────────────────────────────────────────


def test_budget_derives_from_window_and_pct(monkeypatch):
    import graph.model_window as mwin

    monkeypatch.setattr(mwin, "context_window_for", lambda config, model_name=None: 128_000)
    cfg = SimpleNamespace(api_base="http://gw", context_budget_pct=8.0)
    assert ProjectionOptions.from_config(cfg).budget_chars == int(128_000 * 0.08 * 4)
    # A missing attribute → the documented 8% default; 0 → unbounded.
    assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw")).budget_chars == int(128_000 * 0.08 * 4)
    assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct=0)).budget_chars is None
    # 2.5% of 128k = 12 800 chars — below the floor, so the floor applies.
    from graph.projection import _MIN_BUDGET_CHARS

    assert int(128_000 * 0.025 * 4) < _MIN_BUDGET_CHARS
    assert (
        ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct="2.5")).budget_chars
        == _MIN_BUDGET_CHARS
    )
    # 20% of 128k = 102 400 chars — above the floor, the percentage rules.
    assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct=20)).budget_chars == int(
        128_000 * 0.20 * 4
    )


def test_no_window_means_no_budget(monkeypatch):
    import graph.model_window as mwin

    monkeypatch.setattr(mwin, "context_window_for", lambda config, model_name=None: None)
    assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct=8.0)).budget_chars is None
    assert ProjectionOptions.from_config(None).budget_chars is None
    assert ProjectionOptions().budget_chars is None


# ── unbounded = byte-identical legacy shape ───────────────────────────────────


def test_unbounded_sheds_nothing_and_keeps_the_two_key_shape(quiet):
    store = _Store(hot=_HOT, results=_hits(5))
    p = _compose(store, _Index(_SKILLS), budget=None)
    assert p.rag_ids == [1, 2, 3, 4, 5] and p.digest_ids == _DIGEST_IDS and p.hot_ids == [7, 9]
    assert p.budget_chars is None and p.overflow == [] and p.used_chars == len(p.text)
    assert all("truncated" not in s for s in p.sections)
    assert set(p.as_legacy_dict()) == {"context", "context_sections"}


def test_within_budget_sheds_nothing_but_reports_the_ceiling(quiet):
    store = _Store(hot=_HOT, results=_hits(5))
    full = _compose(store, _Index(_SKILLS), budget=None)
    p = _compose(store, _Index(_SKILLS), budget=len(full.text))
    assert p.text == full.text
    assert p.overflow == [] and p.used_chars == len(full.text) and p.budget_chars == len(full.text)
    assert p.as_legacy_dict()["budget"] == {"chars": len(full.text), "used": len(full.text), "overflow": []}
    assert all("truncated" not in s for s in p.sections)


# ── shed order ───────────────────────────────────────────────────────────────


def test_rag_hits_shed_one_by_one_from_the_lowest_ranked_end(quiet):
    store = _Store(hot=_HOT, results=_hits(5))
    index = _Index(_SKILLS)
    two = _compose(_Store(hot=_HOT, results=_hits(2)), index, budget=None)
    p = _compose(store, index, budget=len(two.text))  # exactly two hits fit
    assert p.rag_ids == [1, 2]  # best-ranked survive; 3,4,5 shed
    assert p.text == two.text  # deterministic — identical to composing with two hits
    assert p.overflow == [{"label": "RAG hits", "dropped_items": 3, "dropped_chars": p.overflow[0]["dropped_chars"]}]
    assert p.overflow[0]["dropped_chars"] > 0
    assert p.digest_ids == _DIGEST_IDS and p.hot_ids == [7, 9]  # nothing above RAG touched
    memory = next(s for s in p.sections if s["label"].startswith("Injected memory"))
    assert memory["truncated"] is True and "2 docs" in memory["label"]
    assert "knowledge:2" in p.sources


def test_then_the_digest_then_skill_descriptions_in_that_order(quiet):
    store = _Store(hot=_HOT, results=_hits(3))
    index = _Index(_SKILLS)
    # A budget that fits hot + the skill NAMES but not the digest or any hit.
    floor_only = _compose(_Store(hot=_HOT), index, budget=None, digest=False)
    from graph.projection import _skill_index

    floor_block, _, _ = _skill_index(index, ProjectionOptions(skills_index_chars=8192), bare_only=True)
    # hot + "\n\n" + the name-only index: swap the full skill block's chars for the floor's.
    budget = len(floor_only.text) - floor_only.sections[1]["chars"] + len(floor_block)
    p = _compose(store, index, budget=budget)
    assert [o["label"] for o in p.overflow] == ["RAG hits", "Prior sessions", "Skills index"]
    assert p.rag_ids == [] and p.digest_ids == [] and p.hot_ids == [7, 9]
    assert "deploys go out Fridays" in p.text  # always-on survived
    assert "<available_skills>" in p.text and all(f'name="skill-{i}"' in p.text for i in range(6))  # identities kept
    assert "a fairly long description" not in p.text  # descriptions shed
    assert next(s for s in p.sections if s["label"] == "Skills index")["truncated"] is True
    assert p.overflow[1]["dropped_items"] == 2  # two sessions
    assert p.overflow[2]["dropped_items"] == 6  # six descriptions
    assert len(p.text) <= budget


def test_skills_shrink_to_what_fits_before_the_floor(quiet):
    """Room for SOME descriptions → the index is re-rendered under a smaller cap,
    not thrown straight to name-only rows."""
    store = _Store(hot=_HOT)
    index = _Index(_SKILLS)
    full = _compose(store, index, budget=None, digest=False)
    p = _compose(store, index, budget=len(full.text) - 200, digest=False)
    assert "a fairly long description" in p.text  # some descriptions kept
    assert all(f'name="skill-{i}"' in p.text for i in range(6))
    assert p.overflow == [
        {"label": "Skills index", "dropped_items": p.overflow[0]["dropped_items"], "dropped_chars": p.overflow[0]["dropped_chars"]}
    ]
    assert 0 < p.overflow[0]["dropped_items"] < 6
    assert len(p.text) <= len(full.text) - 200


def test_working_state_and_always_on_are_never_shed(quiet, monkeypatch, caplog):
    store = _Store(hot=_HOT, results=_hits(3))
    index = _Index(_SKILLS)
    with caplog.at_level(logging.WARNING, logger="graph.projection"):
        p = _compose(store, index, budget=10, ws=_WS, monkeypatch=monkeypatch)
    assert "deploys go out Fridays" in p.text and "operator prefers dark mode" in p.text
    assert _WS in p.text
    assert p.hot_ids == [7, 9]
    assert p.rag_ids == [] and p.digest_ids == []
    assert all(f'name="skill-{i}"' in p.text for i in range(6))  # the identity floor, never below
    assert len(p.text) > 10  # delivered anyway
    warnings = [r for r in caplog.records if "never-shed" in r.getMessage()]
    assert len(warnings) == 1
    assert "budget=10" in warnings[0].getMessage()


def test_shedding_is_deterministic(quiet):
    store = _Store(hot=_HOT, results=_hits(5))
    index = _Index(_SKILLS)
    a = _compose(store, index, budget=900)
    b = _compose(store, index, budget=900)
    assert (a.text, a.sections, a.overflow, a.rag_ids) == (b.text, b.sections, b.overflow, b.rag_ids)


def test_injection_record_names_what_was_delivered(quiet):
    """The log says what ENTERED the turn — ids after shedding, not what was retrieved."""
    recorded: list = []
    store = _Store(hot=_HOT, results=_hits(5))
    index = _Index(_SKILLS)
    two = _compose(_Store(hot=_HOT, results=_hits(2)), index, budget=None)
    compose_projected_context(
        "what is the deploy day?",
        store,
        index,
        {"session_id": "s"},
        record=True,
        options=ProjectionOptions(top_k=5, budget_chars=len(two.text)),
        prior_sessions=lambda: (_DIGEST, list(_DIGEST_IDS)),
        record_fn=lambda state, parts, d, h, r: recorded.append((d, h, r)),
    )
    assert recorded == [(_DIGEST_IDS, [7, 9], [1, 2])]


# ── the RAG search is deliverable-only ───────────────────────────────────────


def test_search_passes_deliverable_and_post_filters(quiet):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    rows = _hits(4)
    rows[1]["review_state"] = "rejected"
    rows[2]["expires_at"] = past
    rows[3]["expires_at"] = future
    store = _Store(results=rows)
    p = _compose(store, None, budget=None, digest=False)
    assert store.search_kwargs == [{"k": 5, "deliverable": True}]
    assert p.rag_ids == [1, 4]


def test_old_backend_without_the_kwarg_still_gets_the_rule(quiet):
    rows = _hits(2)
    rows[0]["review_state"] = "rejected"
    store = _Store(results=rows, reject_deliverable_kwarg=True)
    p = _compose(store, None, budget=None, digest=False)
    assert store.search_kwargs == [{"k": 5}]  # retried without the kwarg
    assert p.rag_ids == [2]  # …and the post-filter applied the rule anyway


def test_deliverable_hit():
    assert deliverable_hit({})
    assert deliverable_hit({"review_state": "pending"}) and deliverable_hit({"review_state": "confirmed"})
    assert not deliverable_hit({"review_state": "rejected"})
    assert not deliverable_hit({"expires_at": "2000-01-01T00:00:00+00:00"})
    assert deliverable_hit({"expires_at": "2999-01-01T00:00:00+00:00"})


# ── store-level `deliverable` on every backend ───────────────────────────────


def _seed(store) -> dict[str, int]:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    return {
        "plain": store.add_chunk("alpha plain row", domain="d"),
        "pending": store.add_chunk("alpha pending row", domain="d", review_state="pending"),
        "confirmed": store.add_chunk("alpha confirmed row", domain="d", review_state="confirmed"),
        "rejected": store.add_chunk("alpha rejected row", domain="d", review_state="rejected"),
        "expired": store.add_chunk("alpha expired row", domain="d", expires_at=past),
        "future": store.add_chunk("alpha future row", domain="d", expires_at=future),
    }


def test_deliverable_on_the_plain_store(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    ids = _seed(store)
    eligible = {ids["plain"], ids["pending"], ids["confirmed"], ids["future"]}
    assert {c.id for c in store.list_chunks(deliverable=True)} == eligible
    assert {c.id for c in store.list_chunks()} == set(ids.values())  # memory_list sees everything
    assert {r["id"] for r in store.search("alpha", k=10, deliverable=True)} == eligible
    assert {r["id"] for r in store.search("alpha", k=10)} == set(ids.values())
    store._fts_available = False  # the LIKE fallback path
    assert {r["id"] for r in store.search("alpha", k=10, deliverable=True)} == eligible


def _const_embed(text: str) -> list[float]:
    return [1.0, 0.0]


def test_deliverable_on_the_hybrid_store_filters_both_rankings(tmp_path):
    store = HybridKnowledgeStore(tmp_path / "kb.db", embed_fn=_const_embed)
    ids = _seed(store)
    eligible = {ids["plain"], ids["pending"], ids["confirmed"], ids["future"]}
    assert {r["id"] for r in store.search("alpha", k=10, deliverable=True)} == eligible
    # Vector-only path (no shared tokens): the rejected/expired rows can't surface there either.
    assert {r["id"] for r in store.search("zzzzz", k=10, deliverable=True)} == eligible
    assert {r["id"] for r in store.search("zzzzz", k=10)} == set(ids.values())


def test_deliverable_on_the_layered_store_filters_both_tiers(tmp_path):
    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    layered = LayeredKnowledgeStore(private, commons)
    private.add_chunk("alpha private ok", domain="d")
    private.add_chunk("alpha private rejected", domain="d", review_state="rejected")
    commons.add_chunk("alpha commons ok", domain="d")
    commons.add_chunk("alpha commons rejected", domain="d", review_state="rejected")
    hits = layered.search("alpha", k=10, deliverable=True)
    assert {r["content"] for r in hits} == {"alpha private ok", "alpha commons ok"}
    assert len(layered.search("alpha", k=10)) == 4


# ── always-on is a policy, not a domain ──────────────────────────────────────


def test_always_on_selects_by_delivery_policy(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    hot = store.add_chunk("deploys go out Fridays", domain="hot")  # stamped always on write (D4)
    pinned = store.add_chunk("metric units", domain="preferences", delivery_policy="always")
    store.add_chunk("a retrieved fact", domain="preferences")
    store.add_chunk("a rejected pin", domain="preferences", delivery_policy="always", review_state="rejected")
    store.add_chunk("an expired pin", domain="preferences", delivery_policy="always", expires_at=past)
    assert {cid for cid, _ in store.get_hot_memory_entries()} == {hot, pinned}
    assert "metric units" in store.get_hot_memory() and "a retrieved fact" not in store.get_hot_memory()


def test_hot_write_event_fires_on_the_stored_policy(tmp_path, monkeypatch):
    import knowledge.store as ks

    fired: list[int] = []
    monkeypatch.setattr(ks, "_publish_hot_write", lambda chunk_id, *a, **k: fired.append(chunk_id))
    store = KnowledgeStore(tmp_path / "kb.db")
    hot = store.add_chunk("hot row", domain="hot")
    pinned = store.add_chunk("pinned row", domain="preferences", delivery_policy="always")
    store.add_chunk("plain row", domain="preferences")
    store.add_chunk("on demand", domain="preferences", delivery_policy="on_demand")
    assert fired == [hot, pinned]


def test_snapshot_seed_never_carries_always_on_rows(tmp_path):
    from graph.snapshot_op import collect_knowledge_seed

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("public reference", domain="preferences")
    store.add_chunk("private pin", domain="preferences", delivery_policy="always")
    store.add_chunk("hot row", domain="hot")
    seed = collect_knowledge_seed(store)
    assert "hot" not in seed.docs
    assert "public reference" in seed.docs["preferences"] and "private pin" not in seed.docs["preferences"]
    assert seed.counts["preferences"] == 1


# ── the middleware carries the budget through to the legacy dict ─────────────


def test_middleware_compose_reports_the_budget(quiet):
    from langchain_core.messages import HumanMessage

    from graph.middleware.knowledge import KnowledgeMiddleware

    store = _Store(hot=_HOT, results=_hits(5))
    # 800 chars: room for the always-on envelope (~700) but not for a single hit.
    mw = KnowledgeMiddleware(store, options=ProjectionOptions(top_k=5, budget_chars=800))
    mw._prior_sessions_cache = ""
    mw._prior_sessions_ids = []
    mw._prior_sessions_loaded_at = 10**12
    out = mw.compose_context({"messages": [HumanMessage(content="what is the deploy day?")]}, None, record=False)
    assert set(out) == {"context", "context_sections", "budget"}
    assert out["budget"]["chars"] == 800 and out["budget"]["used"] == len(out["context"]) <= 800
    assert out["budget"]["overflow"] == [
        {"label": "RAG hits", "dropped_items": 5, "dropped_chars": out["budget"]["overflow"][0]["dropped_chars"]}
    ]
    assert "deploys go out Fridays" in out["context"]
    # Unbounded keeps the two-key shape exactly.
    mw2 = KnowledgeMiddleware(store, top_k=5)
    mw2._prior_sessions_cache, mw2._prior_sessions_ids, mw2._prior_sessions_loaded_at = "", [], 10**12
    assert set(mw2.compose_context({"messages": [HumanMessage(content="q")]}, None, record=False)) == {
        "context",
        "context_sections",
    }


def test_empty_projection_keeps_the_legacy_shape():
    assert ProjectedContext().as_legacy_dict() == {"context": "", "context_sections": []}
    assert ProjectedContext(budget_chars=100).as_legacy_dict() == {
        "context": "",
        "context_sections": [],
        "budget": {"chars": 100, "used": 0, "overflow": []},
    }


# ── the skill shed walks by ROWS (monotone, one index read per compose) ───────


def test_a_small_overshoot_drops_exactly_one_description(quiet):
    store = _Store(hot=_HOT)
    index = _Index(_SKILLS)
    full = _compose(store, index, budget=None, digest=False)
    assert _full_rows(full.text) == 6
    for overshoot in (1, 3, 5):
        p = _compose(store, index, budget=len(full.text) - overshoot, digest=False)
        assert _full_rows(p.text) == 5, overshoot
        assert all(f'name="skill-{i}"' in p.text for i in range(6))
        assert p.overflow == [
            {"label": "Skills index", "dropped_items": 1, "dropped_chars": p.overflow[0]["dropped_chars"]}
        ]
        assert len(p.text) <= len(full.text) - overshoot


def test_descriptions_kept_is_monotone_in_the_budget(quiet):
    store = _Store(hot=_HOT)
    index = _Index(_SKILLS)
    full = _compose(store, index, budget=None, digest=False)
    kept = []
    for budget in [*range(len(full.text) - 1400, len(full.text), 23), len(full.text)]:
        p = _compose(store, index, budget=budget, digest=False)
        kept.append(_full_rows(p.text))
        assert all(f'name="skill-{i}"' in p.text for i in range(6))  # identities never drop
    assert kept == sorted(kept)  # non-decreasing in the budget
    assert kept[0] < 6 and kept[-1] == 6


def test_the_skill_index_is_read_once_per_compose(quiet):
    class _Counting(_Index):
        reads = 0

        def skill_summaries(self, limit=None):
            type(self).reads += 1
            return super().skill_summaries(limit)

    index = _Counting(_SKILLS)
    store = _Store(hot=_HOT)
    p = _compose(store, index, budget=10, digest=False)  # everything shed to the floor
    assert _Counting.reads == 1
    assert _full_rows(p.text) == 0 and all(f'name="skill-{i}"' in p.text for i in range(6))


# ── the derived budget has a floor; the warning is heard once ────────────────


def test_budget_never_below_the_floor(monkeypatch):
    import graph.model_window as mwin
    from graph.projection import _MIN_BUDGET_CHARS

    for window, expected in ((8_000, _MIN_BUDGET_CHARS), (32_000, _MIN_BUDGET_CHARS), (128_000, 40_960)):
        monkeypatch.setattr(mwin, "context_window_for", lambda config, model_name=None, w=window: w)
        cfg = SimpleNamespace(api_base="http://gw", context_budget_pct=8.0)
        assert ProjectionOptions.from_config(cfg).budget_chars == expected, window
    assert _MIN_BUDGET_CHARS == 16_000  # ≈ always-on cap 6k + digest cap ~8k + headroom


def test_never_shed_warning_is_heard_once_per_standing_context(quiet, monkeypatch, caplog):
    index = _Index(_SKILLS)
    with caplog.at_level(logging.WARNING, logger="graph.projection"):
        for _ in range(3):
            _compose(_Store(hot=_HOT), index, budget=10, ws=_WS, monkeypatch=monkeypatch)
        # A change in the standing context (a new always-on fact) is heard again.
        _compose(_Store(hot=[*_HOT, (11, "a new standing rule")]), index, budget=10, ws=_WS, monkeypatch=monkeypatch)
    assert len([r for r in caplog.records if "never-shed" in r.getMessage()]) == 2


def test_inert_budget_is_logged_once(quiet, monkeypatch, caplog):
    import graph.model_window as mwin

    monkeypatch.setattr(mwin, "context_window_for", lambda config, model_name=None: None)
    with caplog.at_level(logging.WARNING, logger="graph.projection"):
        for _ in range(3):
            assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct=8.0)).budget_chars is None
    inert = [r for r in caplog.records if "inert" in r.getMessage()]
    assert len(inert) == 1 and "unbounded" in inert[0].getMessage()


# ── shapes ───────────────────────────────────────────────────────────────────


def test_everything_shed_keeps_the_overflow(quiet):
    p = _compose(_Store(results=_hits(3)), None, budget=1, digest=False)
    assert p.text == "" and p.used_chars == 0 and p.sections == [] and p.rag_ids == []
    assert p.overflow == [{"label": "RAG hits", "dropped_items": 3, "dropped_chars": p.overflow[0]["dropped_chars"]}]
    assert p.as_legacy_dict() == {
        "context": "",
        "context_sections": [],
        "budget": {"chars": 1, "used": 0, "overflow": p.overflow},
    }


def test_non_positive_budget_reads_as_unbounded():
    assert ProjectionOptions(budget_chars=0).budget_chars is None
    assert ProjectionOptions(budget_chars=-5).budget_chars is None
    assert ProjectionOptions(budget_chars=1).budget_chars == 1


def test_deliverable_hit_parses_iso_shapes():
    assert deliverable_hit({"expires_at": "2999-01-01T00:00:00Z"})
    assert not deliverable_hit({"expires_at": "2000-01-01T00:00:00Z"})
    assert deliverable_hit({"expires_at": "2999-01-01T00:00:00"})  # naive → UTC
    assert not deliverable_hit({"expires_at": "2000-01-01"})  # a bare date
    assert deliverable_hit({"expires_at": "2999-06-01T12:00:00+02:00"})
    assert deliverable_hit({"expires_at": "not a date"})  # string fallback, never raises


def test_budget_pct_yaml_coercion(tmp_path, caplog):
    from graph.config import LangGraphConfig

    cases = {"blank": ('""', 8.0), "negative": ("-3", 0.0), "text": ("abc", 8.0), "decimal": ("2.5", 2.5)}
    with caplog.at_level(logging.WARNING, logger="graph.config"):
        for name, (raw, expected) in cases.items():
            p = tmp_path / f"{name}.yaml"
            p.write_text(f"context:\n  budget_pct: {raw}\n")
            assert LangGraphConfig.from_yaml(p).context_budget_pct == expected, name
    warned = [r.getMessage() for r in caplog.records if "context.budget_pct" in r.getMessage()]
    assert len(warned) == 2  # blank + text; a negative value is a valid "off"


# ── the budget follows the TURN's model, not the configured default ──────────
#
# The console lets each chat tab pick its own model (ModelOverrideMiddleware
# swaps the LLM off ``state["model"]``), but the graph is compiled once. These
# drive the REAL graph/model_window resolver against a faked gateway rather
# than monkeypatching context_window_for away — stubbing it is precisely how
# the mismatch these cover shipped unnoticed.


class _GatewayResp:
    def __init__(self, payload):
        self._payload = payload

    status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def two_model_gateway(monkeypatch):
    """A gateway reporting a big default model and a small one to switch to."""
    import httpx

    import graph.model_window as mwin

    mwin.reset_window_cache()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, headers=None, timeout=None: _GatewayResp(
            {
                "data": [
                    {"model_name": "protolabs/smart", "model_info": {"max_input_tokens": 196608}},
                    {"model_name": "protolabs/mid", "model_info": {"max_input_tokens": 80000}},
                    {"model_name": "protolabs/fast", "model_info": {"max_input_tokens": 32768}},
                    {"model_name": "protolabs/tiny", "model_info": {"max_input_tokens": 8192}},
                ]
            }
        ),
    )
    yield SimpleNamespace(
        api_base="https://gw.example/v1",
        api_key="sk-test",
        model_name="protolabs/smart",
        context_budget_pct=8.0,
    )
    mwin.reset_window_cache()


def _mw(cfg):
    from graph.middleware.knowledge import KnowledgeMiddleware

    return KnowledgeMiddleware(None, options=ProjectionOptions.from_config(cfg), config=cfg)


def test_budget_and_skill_cap_follow_a_per_chat_model_override(two_model_gateway):
    from graph.projection import _MIN_BUDGET_CHARS

    cfg = two_model_gateway
    mw = _mw(cfg)
    default = mw._options({})
    switched = mw._options({"model": "protolabs/fast"})

    # The configured default is unchanged...
    assert default.budget_chars == 62914 == int(196608 * 0.08 * 4)
    assert default.skills_index_chars == 15728 == int(196608 * 0.02 * 4)
    # ...and a tab on the 32k model no longer carries it: 62 914 chars was ~48%
    # of that model's ENTIRE input window spent on the projection, and a 15 728
    # char skill index on top. 8% of 32 768 lands under the floor, so the budget
    # is the floor — the skill cap, which has none, tracks the window directly.
    assert switched.budget_chars == _MIN_BUDGET_CHARS == 16_000
    assert switched.skills_index_chars == 2621 == int(32768 * 0.02 * 4)
    assert switched.budget_chars < default.budget_chars
    assert switched.skills_index_chars < default.skills_index_chars


def test_the_floor_still_holds_for_a_small_window_override(two_model_gateway):
    from graph.projection import _MIN_BUDGET_CHARS

    # 8% of 8 192 tokens = 2 621 chars — under the floor, so always-on memory and
    # the digest are still never fought over (the D6 contract).
    assert int(8192 * 0.08 * 4) < _MIN_BUDGET_CHARS
    assert _mw(two_model_gateway)._options({"model": "protolabs/tiny"}).budget_chars == _MIN_BUDGET_CHARS


def test_absent_blank_or_unknown_model_reproduces_todays_numbers(two_model_gateway):
    cfg = two_model_gateway
    mw = _mw(cfg)
    built = ProjectionOptions.from_config(cfg)
    for state in ({}, {"model": ""}, {"model": "   "}, {"model": "not/a-model"}, None):
        opts = mw._options(state)
        assert opts.budget_chars == built.budget_chars, state
        assert opts.skills_index_chars == built.skills_index_chars, state
    # A middleware built without a config can't re-derive — the construction-time
    # options stand, exactly as before.
    no_cfg = _mw(cfg)
    no_cfg._config = None
    assert no_cfg._options({"model": "protolabs/fast"}) == built


def test_per_model_options_are_memoized_without_leaking(two_model_gateway):
    mw = _mw(two_model_gateway)
    mid_a = mw._options({"model": "protolabs/mid"})
    mid_b = mw._options({"model": "protolabs/mid"})
    tiny = mw._options({"model": "protolabs/tiny"})
    assert mid_a is mid_b  # memoized per model
    assert tiny is not mid_a
    # 8% of 80k clears the floor; 8% of 8k does not — distinct, not cross-fed.
    assert mid_a.budget_chars == 25_600
    assert tiny.budget_chars == 16_000
    assert set(mw._options_by_model) == {"protolabs/mid", "protolabs/tiny"}


def test_pruning_threshold_follows_the_turns_model(two_model_gateway):
    """The same construction-time-window trap in ToolResultPrunerMiddleware."""
    from graph.middleware.tool_result_pruner import ToolResultPrunerMiddleware
    from graph.model_window import context_window_for

    cfg = two_model_gateway
    mw = ToolResultPrunerMiddleware(
        max_input_tokens=context_window_for(cfg), at_fraction=0.6, config=cfg
    )
    assert mw._threshold_tokens({}) == int(196608 * 0.6)
    assert mw._threshold_tokens({"model": "protolabs/fast"}) == int(32768 * 0.6)
    # Unknown/absent model, or no config: the constructed window stands.
    assert mw._threshold_tokens({"model": "not/a-model"}) == int(196608 * 0.6)
    assert mw._threshold_tokens(None) == int(196608 * 0.6)
