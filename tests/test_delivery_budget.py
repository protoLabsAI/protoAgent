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
    """No working state, no goal turn, no disk: every test states what it wants."""
    import graph.projection as proj

    monkeypatch.setattr(proj, "working_state_block", lambda state: "")
    monkeypatch.setattr(proj, "_in_goal_turn", lambda: False)


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
    assert ProjectionOptions.from_config(SimpleNamespace(api_base="http://gw", context_budget_pct="2.5")).budget_chars == int(
        128_000 * 0.025 * 4
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
