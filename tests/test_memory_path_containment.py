"""A caller-supplied session_id must never build a path outside the memory dir (#2340).

`session_filename` encodes ':' (NTFS) but neutralizes neither separators nor '..', and
session ids arrive from callers on several surfaces — /api/chat, A2A conversation ids,
/v1. The containment check lives at the path layer so no caller has to remember it.
"""

from __future__ import annotations

import json

from graph.middleware.memory import contained_in, session_file_candidates


# ── the containment predicate ─────────────────────────────────────────────────
def test_a_normal_name_is_contained(tmp_path):
    assert contained_in(str(tmp_path), str(tmp_path / "chat-1.json"))


def test_dot_dot_escapes(tmp_path):
    assert not contained_in(str(tmp_path), str(tmp_path / ".." / ".." / "escaped.json"))


def test_an_absolute_path_elsewhere_escapes(tmp_path):
    assert not contained_in(str(tmp_path), "/etc/passwd")


def test_a_sibling_with_a_shared_prefix_escapes(tmp_path):
    """`/memory-evil` must not pass a naive startswith check against `/memory`."""
    base = tmp_path / "memory"
    base.mkdir()
    sibling = tmp_path / "memory-evil"
    sibling.mkdir()

    assert not contained_in(str(base), str(sibling / "x.json"))


def test_a_symlinked_memory_dir_still_compares_correctly(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert contained_in(str(link), str(link / "chat-1.json"))


# ── read candidates ───────────────────────────────────────────────────────────
def test_escaping_ids_yield_no_read_candidates(tmp_path):
    for evil in ("../../escaped", "../sibling", "/etc/passwd", "a/../../b"):
        assert session_file_candidates(evil, base=str(tmp_path)) == [], evil


def test_ordinary_ids_still_get_both_candidates(tmp_path):
    got = session_file_candidates("a2a:abc", base=str(tmp_path))

    assert len(got) == 2
    assert got[0].endswith("a2a%3Aabc.json")  # encoded first
    assert got[1].endswith("a2a:abc.json")  # then the legacy raw-':' name


def test_an_id_needing_no_encoding_yields_one_candidate(tmp_path):
    assert len(session_file_candidates("chat-1", base=str(tmp_path))) == 1


# ── the write path ────────────────────────────────────────────────────────────
def _persist(monkeypatch, tmp_path, session_id):
    from graph.middleware import memory as mem

    monkeypatch.setattr(mem, "memory_path", lambda: str(tmp_path))
    mem._persist_session(
        {"session_id": session_id, "messages": [{"role": "user", "content": "hi"}]},
        "trace-1",
    )


def test_an_escaping_id_is_never_written(monkeypatch, tmp_path):
    """The write used the same unneutralized filename, so this escaped too — not just
    the legacy read the issue originally described."""
    outside = tmp_path.parent / "escaped.json"
    if outside.exists():
        outside.unlink()

    _persist(monkeypatch, tmp_path, "../escaped")

    assert not outside.exists()
    assert list(tmp_path.glob("*.json")) == []


def test_an_ordinary_session_still_persists(monkeypatch, tmp_path):
    _persist(monkeypatch, tmp_path, "chat-42")

    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text())["session_id"] == "chat-42"


def test_a_colon_id_persists_under_the_encoded_name(monkeypatch, tmp_path):
    _persist(monkeypatch, tmp_path, "a2a:xyz")

    names = [p.name for p in tmp_path.glob("*.json")]
    assert names == ["a2a%3Axyz.json"]  # NTFS-safe encoding preserved


def test_the_legacy_unlink_cannot_reach_outside(monkeypatch, tmp_path):
    """After a successful write the legacy raw-':' twin is removed. That unlink took
    the same unneutralized path, so it could have deleted a file outside the dir."""
    victim = tmp_path.parent / "victim.json"
    victim.write_text("{}", encoding="utf-8")

    _persist(monkeypatch, tmp_path, "../victim")

    assert victim.exists()  # untouched
    victim.unlink()
