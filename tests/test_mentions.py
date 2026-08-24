"""`@<name>` mention resolution (#3042) — the addressing twin of slash resolution.

Guards the three properties the dispatcher and the console composer both depend on:
only a LEADING mention addresses a turn, resolution reaches exactly the roster
`delegate_to` reaches, and an unknown token falls through to an ordinary turn instead
of short-circuiting.
"""

from __future__ import annotations

import runtime.state as rs

import graph.mentions as mentions


class _Reg:
    """A stand-in DelegateRegistry — the two methods graph.mentions reads."""

    def __init__(self, entries):
        self._entries = entries

    def names(self):
        return [e["name"] for e in self._entries]

    def roster(self):
        return list(self._entries)


def _install(monkeypatch, entries):
    monkeypatch.setattr(rs.STATE, "delegate_registry", _Reg(entries), raising=False)


PROTO = {"name": "proto", "type": "agent", "description": "the PM", "url": ""}
CODER = {"name": "claude-code", "type": "coding_agent", "description": "coder", "url": ""}


# --- parse_mention -------------------------------------------------------------
# The contract is #3043's: SYNTACTIC only, `None` when there is no leading mention, and
# `(token, "")` for a bare `@name` so the dispatcher can answer with a usage hint rather
# than treat it as a parse failure.


def test_leading_mention_splits_into_token_and_message():
    assert mentions.parse_mention("@proto fix the flaky test") == ("proto", "fix the flaky test")


def test_a_bare_mention_yields_an_empty_message_not_a_parse_failure():
    assert mentions.parse_mention("@proto") == ("proto", "")


def test_mention_further_into_the_message_does_not_address_the_turn():
    """One message goes to ONE participant — a second `@` is prose, not a fan-out."""
    assert mentions.parse_mention("ask @proto about it") is None


def test_email_address_is_not_a_mention():
    assert mentions.parse_mention("mail josh@protolabs.studio about it") is None


def test_leading_whitespace_does_not_hide_a_mention():
    assert mentions.parse_mention("   @proto go") == ("proto", "go")


def test_mention_token_may_carry_hyphens_and_dots():
    assert mentions.parse_mention("@claude-code refactor") == ("claude-code", "refactor")
    assert mentions.parse_mention("@gpt-5.1 summarize") == ("gpt-5.1", "summarize")


def test_a_multiline_message_keeps_its_body():
    assert mentions.parse_mention("@proto line one\nline two") == ("proto", "line one\nline two")


def test_plain_text_is_not_a_mention():
    assert mentions.parse_mention("what about the tests?") is None
    assert mentions.parse_mention("") is None


# --- mention_target -----------------------------------------------------------


def test_exact_name_resolves(monkeypatch):
    _install(monkeypatch, [PROTO, CODER])
    assert mentions.mention_target("proto") == "proto"
    assert mentions.mention_target("claude-code") == "claude-code"


def test_case_insensitive_match_resolves_when_unique(monkeypatch):
    _install(monkeypatch, [PROTO, CODER])
    assert mentions.mention_target("Proto") == "proto"
    assert mentions.mention_target("CLAUDE-CODE") == "claude-code"


def test_ambiguous_case_fold_resolves_to_nothing(monkeypatch):
    """Addressing the WRONG agent is worse than falling through to an ordinary turn."""
    _install(monkeypatch, [{"name": "Proto", "type": "agent", "description": "", "url": ""}, PROTO])
    assert mentions.mention_target("PROTO") is None


def test_unknown_name_resolves_to_nothing(monkeypatch):
    _install(monkeypatch, [PROTO])
    assert mentions.mention_target("nobody") is None


def test_no_delegates_plugin_means_nothing_is_addressable(monkeypatch):
    monkeypatch.setattr(rs.STATE, "delegate_registry", None, raising=False)
    assert mentions.mention_target("proto") is None
    assert mentions.resolve_mentions() == []


def test_a_broken_roster_never_breaks_a_turn(monkeypatch):
    class _Broken:
        def names(self):
            raise RuntimeError("roster exploded")

        def roster(self):
            raise RuntimeError("roster exploded")

    monkeypatch.setattr(rs.STATE, "delegate_registry", _Broken(), raising=False)
    assert mentions.mention_target("proto") is None
    assert mentions.resolve_mentions() == []


# --- resolve_mentions ---------------------------------------------------------


def test_roster_is_offered_with_kind_and_usage(monkeypatch):
    _install(monkeypatch, [PROTO, CODER])
    got = mentions.resolve_mentions()
    assert got == [
        {"name": "proto", "kind": "agent", "description": "the PM", "usage": "@proto …"},
        {"name": "claude-code", "kind": "coding_agent", "description": "coder", "usage": "@claude-code …"},
    ]


def test_every_offered_mention_resolves(monkeypatch):
    """The composer must never offer a token the dispatcher won't route."""
    _install(monkeypatch, [PROTO, CODER])
    for entry in mentions.resolve_mentions():
        assert mentions.mention_target(entry["name"]) == entry["name"]


def test_nameless_roster_entries_are_dropped(monkeypatch):
    _install(monkeypatch, [PROTO, {"name": "", "type": "agent"}, "not-a-dict"])
    assert [e["name"] for e in mentions.resolve_mentions()] == ["proto"]


def test_trailing_punctuation_stays_on_the_token():
    """Documenting #3043's behaviour, not endorsing it: `@proto, what do you think?`
    parses the token as `proto,`, which resolves to nobody and answers with the roster.
    A papercut worth fixing, but not by silently changing the shipped parser here."""
    assert mentions.parse_mention("@proto, what do you think?") == ("proto,", "what do you think?")


def test_a_lone_sigil_is_not_a_mention():
    assert mentions.parse_mention("@") is None
    assert mentions.parse_mention("@ me when it lands") is None
