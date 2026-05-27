"""Tests for RAG guardrails (grade_document / filter_relevant / rewrite_query)."""

import pytest

from graph.guardrails import filter_relevant, grade_document, rewrite_query


@pytest.mark.asyncio
async def test_short_content_rejected():
    called = []
    assert await grade_document("q", "tiny", grade_fn=lambda *a: called.append(1) or "yes") is False
    assert not called  # grade_fn not even consulted for too-short content


@pytest.mark.asyncio
async def test_grade_yes_no():
    yes = lambda q, c: "Yes, relevant"
    no = lambda q, c: "no"
    content = "x" * 100
    assert await grade_document("q", content, grade_fn=yes) is True
    assert await grade_document("q", content, grade_fn=no) is False


@pytest.mark.asyncio
async def test_grade_accepts_bool_and_async():
    content = "x" * 100
    assert await grade_document("q", content, grade_fn=lambda q, c: True) is True
    async def agrade(q, c):
        return "yes"
    assert await grade_document("q", content, grade_fn=agrade) is True


@pytest.mark.asyncio
async def test_grade_fails_open():
    def boom(q, c):
        raise RuntimeError("llm down")
    # error → keep the doc (fail open)
    assert await grade_document("q", "x" * 100, grade_fn=boom) is True


@pytest.mark.asyncio
async def test_filter_relevant_with_key():
    docs = [{"preview": "a" * 100}, {"preview": "b" * 100}]
    grade_fn = lambda q, c: "yes" if c.startswith("a") else "no"
    kept = await filter_relevant("q", docs, grade_fn=grade_fn, key=lambda d: d["preview"])
    assert kept == [docs[0]]


@pytest.mark.asyncio
async def test_rewrite_query():
    assert await rewrite_query("moe", rewrite_fn=lambda q: "mixture of experts") == "mixture of experts"
    # empty result → original
    assert await rewrite_query("moe", rewrite_fn=lambda q: "  ") == "moe"
    # error → original
    def boom(q):
        raise RuntimeError("x")
    assert await rewrite_query("moe", rewrite_fn=boom) == "moe"
