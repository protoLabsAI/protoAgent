"""History-store retention (ADR 0125 history table): per-class age-out, dry run by default.

The 2026-11-08 audit found one agent's history holding 564 rows of system:activity
archives from a single compaction burst and 197 background reports from August, with no
way to age either out. Operator chat archives are kept unless the operator opts in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from graph.config import LangGraphConfig
from knowledge.history import HistoryStore

NOW = datetime(2026, 11, 8, tzinfo=UTC)
OLD = (NOW - timedelta(days=200)).isoformat()
NEW = (NOW - timedelta(days=1)).isoformat()


def _seed(store: HistoryStore) -> dict[str, int]:
    ids = {
        "chat_archive_old": store.add("chat", session_id="chat-1", kind="archive", created_at=OLD),
        "activity_old": store.add("wake", session_id="system:activity", kind="archive", created_at=OLD),
        "activity_new": store.add("wake", session_id="system:activity", kind="archive", created_at=NEW),
        "report_old": store.add("report", session_id="chat-1", kind="report", created_at=OLD),
        "report_new": store.add("report", session_id="chat-1", kind="report", created_at=NEW),
        "summary_old": store.add("sum", session_id="a2a:peer", kind="summary", created_at=OLD),
    }
    return ids


def _defaults() -> dict[str, int]:
    cfg = LangGraphConfig()
    return {n: getattr(cfg, f"history_{n}_retention_days") for n in ("system", "report", "summary", "archive")}


def test_defaults_are_a_dry_run_that_keeps_chat_archives_and_summaries():
    cfg = LangGraphConfig()
    assert cfg.history_retention_dry_run is True
    assert _defaults() == {"system": 30, "report": 90, "summary": 0, "archive": 0}


def test_dry_run_counts_and_deletes_nothing(tmp_path):
    store = HistoryStore(tmp_path / "agent.db")
    ids = _seed(store)
    assert store.prune(_defaults(), now=NOW) == {"system": 1, "report": 1}
    assert all(store.get(i) is not None for i in ids.values())


def test_apply_removes_only_the_aged_classes(tmp_path):
    store = HistoryStore(tmp_path / "agent.db")
    ids = _seed(store)
    assert store.prune(_defaults(), dry_run=False, now=NOW) == {"system": 1, "report": 1}
    gone = {name for name, i in ids.items() if store.get(i) is None}
    assert gone == {"activity_old", "report_old"}  # chat archive and summary kept


def test_opt_in_classes_and_held_rows(tmp_path):
    store = HistoryStore(tmp_path / "agent.db")
    ids = _seed(store)
    store.mark_forget_pending("forget_pending:x", session_id="chat-1")  # a forget in flight
    counts = store.prune({"archive": 30, "summary": 30}, dry_run=False, now=NOW)
    assert counts == {"summary": 1, "archive": 0}  # the held chat archive is left to its forget
    assert store.get(ids["summary_old"]) is None
    assert store.prune({}, dry_run=False, now=NOW) == {}


def test_config_parses_the_history_and_session_keys(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "memory:\n  session_retention_days: 0\n  session_max_files: -5\n"
        "history:\n  retention_dry_run: false\n  report_retention_days: 7\n  archive_retention_days: nope\n"
    )
    cfg = LangGraphConfig.from_yaml(str(path))
    assert cfg.memory_session_retention_days == 0  # 0 = off is allowed
    assert cfg.memory_session_max_files == 500  # negative falls back to the default
    assert cfg.history_retention_dry_run is False
    assert cfg.history_report_retention_days == 7
    assert cfg.history_archive_retention_days == 0  # non-numeric falls back to the default
