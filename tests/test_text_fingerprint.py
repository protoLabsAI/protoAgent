"""The answer-text fingerprint the executor stamps on a terminal artifact
(``TEXT_FINGERPRINT_META``) is taken over a MEDIA-NORMALISED copy of the text, not the raw
streamed text (#4274). A member turn's final answer is rewritten with expiring absolute media
links (``https://hub/agents/<slug>/media/<file>?exp=…&sig=…``), so the stored text no longer
equalled the streamed text and the raw fingerprint missed, folding the console's interleaving
back to frames-then-text on reload. Normalising both sides to a bare ``/media/<file>`` makes
them compare equal again.

``SHARED_VECTORS`` is the parity list: the identical inputs/outputs are asserted against the
TypeScript ``normaliseMediaText`` in ``apps/web/src/chat/sessionHydration.test.ts`` — the two
normalisers must agree byte for byte or the fingerprint a server stamps won't verify in the
browser."""

from __future__ import annotations

from a2a_impl.executor import (
    TEXT_FINGERPRINT_META,
    _normalise_media_text,
    _text_fingerprint_meta,
    _text_offset_meta,
    _utf16_fnv1a,
)

# (input, expected) — kept IN SYNC with SHARED_VECTORS in sessionHydration.test.ts.
SHARED_VECTORS = [
    # A streamed relative link and the stored absolute rewritten link collapse to the same thing.
    ("/media/a.png?sig=x", "/media/a.png"),
    ("https://hub/agents/m/media/a.png?exp=1&sig=y", "/media/a.png"),
    # Absolute without the agent slug, and relative with it, both reach the same canonical form.
    ("https://hub/media/a.png", "/media/a.png"),
    ("/agents/m/media/a.png?x=1", "/media/a.png"),
    # Already-canonical is a no-op (normalisation is idempotent).
    ("/media/a.png", "/media/a.png"),
    # A nested media path keeps its sub-path, only the query/host/slug go.
    ("/media/sub/dir/file.png?sig=x", "/media/sub/dir/file.png"),
    # Several links in one body, in markdown-image and html-src positions.
    (
        "See ![img](/media/a.png?sig=x) and ![b](https://hub/agents/z/media/b.jpg?exp=2&sig=w) here",
        "See ![img](/media/a.png) and ![b](/media/b.jpg) here",
    ),
    ('<img src="https://hub/agents/m/media/a.png?exp=1&sig=y">', '<img src="/media/a.png">'),
    # Non-media URLs are untouched — no `/media/` segment.
    ("https://example.com/foo/bar?x=1", "https://example.com/foo/bar?x=1"),
    # An `/agents/<slug>` path that is NOT a media URL is left alone.
    ("/agents/m/profile?x=1", "/agents/m/profile?x=1"),
    # Plain prose with no URL at all.
    ("no urls here, just text.", "no urls here, just text."),
    ("😀 https://hub/agents/m/media/a.png?sig=x done", "😀 /media/a.png done"),
]


def test_normalise_media_text_shared_vectors():
    for raw, expected in SHARED_VECTORS:
        assert _normalise_media_text(raw) == expected, raw


def test_normalise_is_idempotent():
    for raw, _expected in SHARED_VECTORS:
        once = _normalise_media_text(raw)
        assert _normalise_media_text(once) == once, raw


def test_fingerprint_is_v2_over_the_normalised_text():
    """The stamped fingerprint describes the NORMALISED text and carries the v:2 scheme
    marker, so the console knows to normalise before comparing."""
    streamed = "Here is the image:\n\n![img](/media/a.png?sig=x)"
    normalised = "Here is the image:\n\n![img](/media/a.png)"
    assert _normalise_media_text(streamed) == normalised
    meta = _text_fingerprint_meta(streamed)[TEXT_FINGERPRINT_META]
    assert meta == {
        "v": 2,
        "length": len(normalised.encode("utf-16-le")) // 2,
        "fnv1a": _utf16_fnv1a(normalised),
    }


def test_rewritten_absolute_link_matches_the_fingerprint():
    """What the console does on reload: a member turn's stored text carries absolute expiring
    links, but its normalised prefix still hashes to the fingerprint the executor stamped over
    the streamed (relative-link) text — so interleaving survives."""
    streamed = "Here is the image:\n\n![img](/media/a.png?sig=x)"
    stored = "Here is the image:\n\n![img](https://hub/agents/m/media/a.png?exp=1&sig=y)"
    meta = _text_fingerprint_meta(streamed)[TEXT_FINGERPRINT_META]
    measured = _normalise_media_text(stored)
    # ASCII here, so a code-unit slice is a plain character slice — this is exactly what the
    # console computes with measured.slice(0, fingerprint.length).
    assert meta["length"] <= len(measured)
    assert _utf16_fnv1a(measured[: meta["length"]]) == meta["fnv1a"]


def test_non_media_url_does_not_match_a_media_fingerprint():
    """A non-media URL isn't normalised, so a turn whose stored text diverges on a NON-media
    URL still (correctly) fails the fingerprint and falls back — normalisation only forgives
    media-link rewrites, not arbitrary edits."""
    streamed = "See https://example.com/a?x=1 done"
    stored = "See https://example.com/a?x=2 done"
    meta = _text_fingerprint_meta(streamed)[TEXT_FINGERPRINT_META]
    measured = _normalise_media_text(stored)
    assert _utf16_fnv1a(measured[: meta["length"]]) != meta["fnv1a"]


def test_offsets_preserve_raw_and_normalised_utf16_boundaries():
    text = "😀 ![img](/media/a.png?sig=x) done"
    assert _text_offset_meta(text) == {
        "protoagent/textOffset": len(text.encode("utf-16-le")) // 2,
        "protoagent/normalisedTextOffset": len(_normalise_media_text(text).encode("utf-16-le")) // 2,
    }
