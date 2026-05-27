"""Tests for the SHA256/TTL ResponseCache."""

from cache import ResponseCache


def test_set_get_roundtrip(tmp_path):
    c = ResponseCache(tmp_path / "c.db")
    assert c.get("q") is None
    c.set("answer", "q")
    assert c.get("q") == "answer"


def test_key_normalizes_case_and_whitespace(tmp_path):
    c = ResponseCache(tmp_path / "c.db")
    c.set("v", "  Hello World ")
    assert c.get("hello world") == "v"


def test_multipart_key_is_distinct(tmp_path):
    c = ResponseCache(tmp_path / "c.db")
    c.set("a", "q", "ctx1")
    c.set("b", "q", "ctx2")
    assert c.get("q", "ctx1") == "a"
    assert c.get("q", "ctx2") == "b"
    assert c.get("q") is None  # different arity → different key


def test_ttl_expiry(tmp_path):
    c = ResponseCache(tmp_path / "c.db", ttl_seconds=0)
    c.set("v", "q")
    assert c.get("q") is None  # ttl=0 → immediately expired


def test_clear(tmp_path):
    c = ResponseCache(tmp_path / "c.db")
    c.set("v", "q")
    c.clear()
    assert c.get("q") is None


def test_unwritable_path_degrades_gracefully(tmp_path, monkeypatch):
    # Force the configured path AND the home fallback to be unwritable;
    # the cache must construct + no-op without raising.
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    c = ResponseCache("/definitely/not/writable/c.db")
    c.set("v", "q")          # no raise
    assert c.get("q") is None
