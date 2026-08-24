"""The doubled-ACP-reply collapse (observed live 2026-08-24).

claude-agent-acp turns intermittently deliver the whole final message twice on ONE
`session/prompt` — `REVIEWERREVIEWER`, a full status block glued to itself. The collapse
is deliberately the narrowest test that catches it: exact halves, no joiner, half ≥ 8.
These pin both directions — the bug collapses, and everything a model might legitimately
say survives untouched.
"""

from __future__ import annotations

import logging

from plugins.coding_agent.acp_client import AcpClient


def _client() -> AcpClient:
    c = AcpClient.__new__(AcpClient)  # no subprocess — only _collapse_doubled's fields
    c.name = "t"
    c._turn_session_id = "sess-1"
    return c


def test_an_exact_doubled_reply_collapses():
    assert _client()._collapse_doubled("REVIEWERREVIEWER") == "REVIEWER"


def test_a_doubled_status_block_collapses():
    block = "## Status\nqueue empty\nblockers: none\n"
    assert _client()._collapse_doubled(block + block) == block


def test_the_collapse_warns_so_occurrences_stay_countable(caplog):
    with caplog.at_level(logging.WARNING):
        _client()._collapse_doubled("mango-mango-mango-mango-")  # halves equal, ≥8
    assert any("doubled reply collapsed" in r.message for r in caplog.records)


def test_a_normal_reply_is_untouched():
    for text in (
        "PING",
        "the fix is on line 40",
        "PING PING",  # deliberate repeat WITH a separator — halves differ
        "say it twice: HELLO HELLO",
        "",
    ):
        assert _client()._collapse_doubled(text) == text


def test_a_short_chant_is_untouched():
    # halves equal but under the 8-char floor — "hahahaha" is something a model says
    assert _client()._collapse_doubled("hahahaha") == "hahahaha"
    assert _client()._collapse_doubled("abab") == "abab"


def test_odd_length_never_collapses():
    assert _client()._collapse_doubled("aaaaaaaaaaaaaaaaa") == "aaaaaaaaaaaaaaaaa"  # 17 chars


def test_a_triple_is_not_a_double_and_survives():
    # X*3 has unequal halves — out of scope on purpose; collapsing it would be guessing.
    t = "REVIEWER" * 3
    assert _client()._collapse_doubled(t) == t
