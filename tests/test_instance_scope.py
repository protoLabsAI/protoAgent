"""Default instance scoping (#706) — never silently share the root."""

from __future__ import annotations

import paths


def test_explicit_instance_always_wins(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    monkeypatch.setenv("PROTOAGENT_AUTO_SCOPE", "1")
    assert paths.instance_id() == "alice"


def test_default_is_unscoped_without_auto(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    assert paths.instance_id() == ""  # legacy behavior preserved (no breakage)


def test_auto_scope_derives_stable_per_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.setenv("PROTOAGENT_AUTO_SCOPE", "1")
    monkeypatch.chdir(tmp_path)
    iid = paths.instance_id()
    assert iid and iid == paths.instance_id()       # non-empty + stable across calls
    sub = tmp_path / "sub"; sub.mkdir(); monkeypatch.chdir(sub)
    assert paths.instance_id() != iid               # a different dir → its own scope


def test_unscoped_warning_only_when_root_has_state(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert paths.unscoped_warning() is None          # empty home → quiet
    (tmp_path / "checkpoints.db").write_text("x")
    assert "UNSCOPED" in (paths.unscoped_warning() or "")
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    assert paths.unscoped_warning() is None           # scoped → quiet
