"""Prod-readiness: the audit log is instance-scoped, rotates at a size cap, and
get_recent reads only a bounded tail (no unbounded growth / full-file OOM)."""

import json

from audit import AuditLogger


def _log_n(a: AuditLogger, n: int):
    for i in range(n):
        a.log(session_id="s", tool=f"t{i}", args={}, result_summary="ok", duration_ms=1, success=True)


def test_writes_and_get_recent_tail(tmp_path):
    a = AuditLogger(path=tmp_path / "audit.jsonl")
    _log_n(a, 50)
    recent = a.get_recent(n=10)
    assert len(recent) == 10
    assert recent[-1]["tool"] == "t49"  # newest last, chronological
    assert all(set(e) >= {"ts", "tool", "session_id"} for e in recent)


def test_rotates_at_size_cap(tmp_path, monkeypatch):
    import audit
    monkeypatch.setattr(audit, "_MAX_BYTES", 2_000)  # tiny cap to force a rotation
    a = AuditLogger(path=tmp_path / "audit.jsonl")
    _log_n(a, 200)
    live = tmp_path / "audit.jsonl"
    backup = tmp_path / "audit.jsonl.1"
    assert live.exists()
    assert backup.exists(), "should have rotated a .1 backup"
    # the live file stays bounded (≈ one cap worth), not the full 200 lines
    assert live.stat().st_size <= 2_000 + 4_000


def test_session_stats_capped(tmp_path, monkeypatch):
    import audit
    monkeypatch.setattr(audit, "_MAX_SESSIONS", 5)
    a = AuditLogger(path=tmp_path / "audit.jsonl")
    for i in range(20):
        a.log(session_id=f"sess-{i}", tool="t", args={}, result_summary="", duration_ms=1, success=True)
    assert len(a._session_stats) <= 5  # oldest evicted


def test_instance_scoping(tmp_path, monkeypatch):
    # PROTOAGENT_INSTANCE namespaces the path under an instance segment.
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "inst-a")
    a = AuditLogger(path=tmp_path / "audit.jsonl")
    a.log(session_id="s", tool="t", args={}, result_summary="", duration_ms=1, success=True)
    assert "inst-a" in str(a.path)
    assert json.loads(a.path.read_text().splitlines()[0])["tool"] == "t"
