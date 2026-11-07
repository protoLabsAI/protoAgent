"""ADR 0125 P3-C: retirement and agent facts share one atomic subject funnel."""

import asyncio
import logging

import pytest

from graph.memory_facts import _parse_facts, consolidate_and_store, extract_and_store_facts
from knowledge.memories import MemoryStore


def _seed(store, *, provenance="agent", review_state=None, valid_from=None):
    rid = store.add(
        "$120K base",
        kind="fact",
        subject="comp-floor",
        provenance=provenance,
        review_state=review_state,
        scope="jobCoach",
    )
    with store._write() as db:
        db.execute(
            "UPDATE memories SET created_at = ?, valid_from = ? WHERE id = ?",
            ("2026-10-16T00:00:00+00:00", valid_from, rid),
        )
    return rid


def _extract(store, as_of, content="$185K", subject="comp-floor"):
    async def extractor(transcript, config):
        return [{"subject": subject, "content": content}]

    return asyncio.run(
        extract_and_store_facts(
            "compensation decision",
            memory_store=store,
            config=object(),
            namespace="jobCoach",
            source="retired-thread",
            extractor=extractor,
            as_of=as_of,
        )
    )


def test_jobcoach_stale_fact_is_audit_only(tmp_path, caplog):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store)
    with caplog.at_level(logging.INFO):
        counts = _extract(store, "2026-10-15")
    assert counts == {"added": 1, "skipped": 0, "superseded": 0}
    assert [r.id for r in store.list()] == [old_id]
    audit = store.list(include_superseded=True)
    assert len(audit) == 2
    incoming = next(r for r in audit if r.id != old_id)
    assert incoming.superseded_by == old_id
    assert incoming.content == "[as of 2026-10-15] $185K"
    assert (incoming.kind, incoming.provenance, incoming.review_state) == ("fact", "extracted", "pending")
    assert incoming.valid_from == "2026-10-15"
    assert incoming.source_session == incoming.thread_id == "retired-thread"
    assert store.search("185K", include_superseded=True, deliverable=True) == []
    for detail in (
        f"old={old_id}",
        f"new={incoming.id}",
        "provenance=agent",
        "provenance=extracted",
        "2026-10-16",
        "2026-10-15",
        "reason=stale",
    ):
        assert detail in caplog.text


@pytest.mark.parametrize("as_of", ["2026-10-16", "2026-10-17"])
def test_at_least_as_new_fact_supersedes_agent(tmp_path, as_of, caplog):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store)
    with caplog.at_level(logging.INFO):
        counts = _extract(store, as_of, subject="  COMP-FLOOR  ")
    assert counts["superseded"] == 1
    live = store.list()
    assert len(live) == 1
    assert store.get(old_id).superseded_by == live[0].id
    assert len(store.list(include_superseded=True)) == 2
    for detail in (
        f"old={old_id}",
        f"new={live[0].id}",
        "provenance=agent",
        "provenance=extracted",
        "2026-10-16",
        as_of,
    ):
        assert detail in caplog.text


def test_confirmed_operator_wins_even_against_newer_extraction(tmp_path, caplog):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store, provenance="operator")
    with caplog.at_level(logging.INFO):
        assert _extract(store, "2026-10-20")["superseded"] == 0
    assert [r.id for r in store.list()] == [old_id]
    assert store.list(include_superseded=True)[0].superseded_by == old_id
    assert "reason=confirmed-operator" in caplog.text


@pytest.mark.parametrize("provenance", ["agent", "extracted", "operator"])
def test_unchanged_details_do_not_write(tmp_path, provenance):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store, provenance=provenance)
    assert _extract(store, "2026-10-20", content="$120K base") == {
        "added": 0,
        "skipped": 1,
        "superseded": 0,
    }
    assert [r.id for r in store.list(include_superseded=True)] == [old_id]


@pytest.mark.parametrize(
    "valid_from, supersedes",
    [
        ("2026-10-14", True),
        ("2026-10-18", False),
        ("2026-10-15T01:00:00+02:00", True),
    ],
)
def test_valid_from_takes_precedence_over_created_at(tmp_path, valid_from, supersedes):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store, valid_from=valid_from)
    counts = _extract(store, "2026-10-15")
    assert counts["superseded"] == int(supersedes)
    assert len(store.list()) == 1
    assert (store.get(old_id).superseded_by is not None) == supersedes


def test_harvest_time_does_not_override_conversation_date(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    _extract(store, "2026-10-15")
    assert _extract(store, "2026-10-16", content="$120K base")["superseded"] == 1
    assert _extract(store, "2026-10-14", content="$200K")["superseded"] == 0
    assert len(store.list()) == 1
    assert len(store.list(include_superseded=True)) == 3


def test_blocker_prevents_partial_retirement_of_multiple_live_rows(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    protected = _seed(store, provenance="operator")
    pending = store.add("$100K", kind="fact", subject="comp-floor", provenance="agent", scope="jobCoach")
    _extract(store, "2026-10-20")
    assert {r.id for r in store.list()} == {protected, pending}
    assert store.list(include_superseded=True)[0].superseded_by == protected


def test_plain_agent_saves_keep_existing_funnel_behavior(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = _seed(store, provenance="operator")
    new_id = store.add(
        "$185K", kind="fact", subject="comp-floor", scope="jobCoach", provenance="agent", review_state="confirmed"
    )
    assert store.get(old_id).superseded_by == new_id


def test_subject_kind_and_scope_remain_isolated(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    ids = [
        store.add("$120K base", kind=kind, subject=subject, scope=scope, provenance="agent")
        for kind, subject, scope in [
            ("decision", "comp-floor", "jobCoach"),
            ("fact", "comp-ceiling", "jobCoach"),
            ("fact", "comp-floor", "other"),
        ]
    ]
    assert _extract(store, "2026-10-20")["superseded"] == 0
    assert all(store.get(rid).superseded_by is None for rid in ids)


def test_structured_parser_and_plain_string_compatibility():
    assert _parse_facts(
        '[{"subject":"comp-floor","content":"$185K"}, "legacy fact", {"subject": 2, "content":"bad"}]'
    ) == [
        {"subject": "comp-floor", "content": "$185K"},
        "legacy fact",
    ]


def test_legacy_string_can_match_agent_subject_by_details(tmp_path):
    store = MemoryStore(tmp_path / "agent.db")
    old_id = store.add(
        "operator deploys on fridays after standup", kind="fact", subject="deploy-time", provenance="agent"
    )
    counts = consolidate_and_store(store, ["operator deploys on fridays after lunch"])
    assert counts["superseded"] == 1
    assert store.get(old_id).superseded_by == store.list()[0].id


@pytest.mark.parametrize("stored_scope, namespace", [(None, ""), ("", None)])
def test_empty_and_null_scope_share_dedup(tmp_path, stored_scope, namespace):
    store = MemoryStore(tmp_path / "agent.db")
    store.add("$120K base", kind="fact", subject="comp-floor", provenance="agent", scope=stored_scope)
    counts = consolidate_and_store(
        store, [{"subject": "comp-floor", "content": "$120K base"}], namespace=namespace,
    )
    assert counts == {"added": 0, "skipped": 1, "superseded": 0}
    assert len(store.list(include_superseded=True)) == 1
