"""Type-ahead recall (#3293) — ``KnowledgeStore.search(prefix=True)``.

The ⌘K palette re-searches on every keystroke, so it asks a question the recall paths
never do: *does a partial word find anything?* Against the shipped default it did not.
``_search_fts`` quotes each token as an FTS5 **phrase** (``_fts_quote``), and phrase
matching is whole-token — so ``postg`` matched nothing while ``postgres`` matched — and
semantic recall, which would have covered it, is ``embeddings: false`` by default. The
operator got an empty shortlist for every character of every word they typed, and an empty
shortlist is indistinguishable from "nothing matched".

These pin the fix and its blast radius: the trailing token is widened, the earlier ones are
not, the flag is OFF for everyone who did not ask (recall, injection, the browser), and the
FTS5 operator characters ``_fts_quote`` exists to neutralise stay neutralised.
"""

from __future__ import annotations

import pytest

from knowledge.store import KnowledgeStore


@pytest.fixture
def store(tmp_path) -> KnowledgeStore:
    s = KnowledgeStore(tmp_path / "kb.db")
    assert s._fts_available, "these pin the FTS5 path; the LIKE fallback is a separate case"
    s.add_chunk("shared_buffers should be a quarter of RAM", heading="Postgres tuning")
    s.add_chunk("drain the node before a rolling restart", heading="Kubernetes runbook")
    return s


def _headings(rows) -> set[str]:
    return {r["heading"] for r in rows}


# ── the defect ───────────────────────────────────────────────────────────────────


def test_partial_word_finds_nothing_without_the_flag(store):
    """The behaviour that made "searches while you type" untrue — kept as the baseline
    so the fix cannot be quietly reverted into a default."""
    for partial in ("po", "pos", "postg", "tun", "kube"):
        assert store.search(partial) == [], f"{partial!r} unexpectedly matched whole-word FTS"
    # …while the finished word always did.
    assert _headings(store.search("postgres")) == {"Postgres tuning"}


def test_prefix_finds_the_word_the_operator_is_still_typing(store):
    """Every prefix of an indexed word reaches its chunk — the property a type-ahead needs
    and the one the docs promise."""
    for partial in ("p", "po", "pos", "postg", "postgres"):
        assert _headings(store.search(partial, prefix=True)) == {"Postgres tuning"}, partial
    for partial in ("k", "kube", "kubernetes"):
        assert _headings(store.search(partial, prefix=True)) == {"Kubernetes runbook"}, partial


def test_prefix_widens_only_the_trailing_token(store):
    """The earlier tokens are words the operator FINISHED; widening those adds noise the
    operator did not ask for. ``postg`` LEADING stays a whole-token phrase and so matches
    nothing — only ``restart``, the word being typed, is widened."""
    assert _headings(store.search("postg restart", prefix=True)) == {"Kubernetes runbook"}
    # Reversed, ``postg`` IS the trailing token and pulls its own chunk in.
    assert _headings(store.search("restart postg", prefix=True)) == {
        "Kubernetes runbook",
        "Postgres tuning",
    }


# ── blast radius ─────────────────────────────────────────────────────────────────


def test_prefix_is_off_by_default_for_every_other_caller(store):
    """``memory_recall``, the per-turn injection and the Knowledge browser all search a
    SETTLED query. Broadening one silently would change what the agent recalls."""
    assert store.search("postg") == []
    assert store.search("postg", prefix=False) == []


def test_prefix_still_quotes_the_token(store):
    """``_fts_quote`` exists so a query cannot smuggle FTS5 syntax; the ``*`` is appended
    OUTSIDE the quotes, so the token stays a literal phrase. A query of pure operator
    characters tokenizes to nothing and must return [] rather than raise."""
    assert store.search('post" OR "', prefix=True) == []  # the quote is not syntax
    assert store.search("*", prefix=True) == []  # tokenizes to nothing
    assert store.search("NEAR OR AND", prefix=True) == []  # bare operators match no chunk


def test_prefix_is_a_noop_on_the_like_fallback(store):
    """The non-FTS backend is substring-matching already, so it matched prefixes all
    along — and must keep answering identically with the flag either way."""
    store._fts_available = False
    assert _headings(store.search("postg")) == {"Postgres tuning"}
    assert _headings(store.search("postg", prefix=True)) == {"Postgres tuning"}


def test_layered_store_passes_prefix_to_both_tiers(tmp_path):
    """A layered store (ADR 0041) fans the search out; a tier that lost the flag would
    drop out of the type-ahead's results entirely."""
    from knowledge.layered import LayeredKnowledgeStore

    private = KnowledgeStore(tmp_path / "private.db")
    commons = KnowledgeStore(tmp_path / "commons.db")
    private.add_chunk("private note about postgres tuning", heading="Private")
    commons.add_chunk("commons note about postgres tuning", heading="Commons")
    layered = LayeredKnowledgeStore(private, commons)

    assert layered.search("postg") == []
    assert _headings(layered.search("postg", prefix=True)) == {"Private", "Commons"}
