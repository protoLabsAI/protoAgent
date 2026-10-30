"""Tests for scripts/evals_ab.py — the learned-context A/B (ADR 0126 D6).

No live LLM and no agent boot: the sweep is faked at the subprocess boundary
(``subprocess.Popen`` for the server, ``subprocess.run`` for the runner,
``_wait_healthy`` for the health poll), so the *real* copy/seed logic runs while
nothing talks to a model. The live path is covered manually via
``python scripts/evals_ab.py --ab-learned-from <instance>``.

What is pinned here is the contract the card owns:

* the source instance's stores are byte-identical (content hash AND mtime) after
  a full A/B run — it is copied, never written;
* arm B holds a REAL copy of those stores (not a symlink back to the source);
* arm A gets no learned memories or skills;
* both arm reports (``ab-A-<ts>.json`` / ``ab-B-<ts>.json``) are written.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import types
from pathlib import Path

import pytest

from evals import sweep
from scripts import evals_ab


# ── fixtures ──────────────────────────────────────────────────────────────────


def _write_db(path: Path, table: str, rows: list[tuple[int, str]]) -> None:
    """A tiny self-contained SQLite store so the backup-API copy has real content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER, note TEXT)")
        conn.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _make_source(root: Path) -> None:
    """A source instance carrying every learned store the A/B copies: the memory
    chunk DB, session summaries, the private skills index + its artifacts, and a
    per-instance cases file (ADR 0126 D5)."""
    _write_db(root / "knowledge" / "agent.db", "memories", [(1, "learned: user prefers tabs")])
    (root / "memory").mkdir(parents=True)
    (root / "memory" / "sess-1.json").write_text(json.dumps({"summary": "shipped the thing"}))
    _write_db(root / "skills.db", "skills", [(1, "deploy-runbook")])
    (root / "skills" / "deploy").mkdir(parents=True)
    (root / "skills" / "deploy" / "SKILL.md").write_text("# Deploy\nRun the thing.\n")
    cases = root / "config" / "evals" / "cases.json"
    cases.parent.mkdir(parents=True)
    cases.write_text(json.dumps([{"id": "instance:deploy_check", "category": "tool", "prompt": "deploy it"}]))


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``root`` → (sha256, mtime_ns). Captures both content and the
    file set, so a stray ``-wal``/``-journal`` sibling or a mutated byte both show."""
    snap: dict[str, tuple[str, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(root))] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return snap


def _fake_sweep_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake only the subprocess boundary of ``evals.sweep``: no server boots, no
    runner runs, the health poll returns immediately, and the 'runner' writes a
    minimal passing report to its ``--out`` path."""

    class FakeProc:
        def __init__(self, *a, **k) -> None:
            pass

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    def fake_run(cmd, **kwargs):
        if "--out" in cmd:
            out = Path(cmd[cmd.index("--out") + 1])
            label = cmd[cmd.index("--model-label") + 1] if "--model-label" in cmd else "?"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "model": label,
                        "total": 1,
                        "passed": 1,
                        "results": [{"id": "c1", "category": "tool", "passed": True}],
                    }
                ),
                encoding="utf-8",
            )
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sweep.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sweep, "_wait_healthy", lambda base_url, *a, **k: {"model": "fake/model"})
    monkeypatch.setattr(sweep.subprocess, "run", fake_run)


@pytest.fixture
def ab_run(tmp_path, monkeypatch):
    """Run a full A/B (both arms, faked) against a fresh source instance, keeping the
    arm instance dirs so the copy/seed can be inspected. Returns the knobs each test
    needs."""
    results = tmp_path / "results"
    monkeypatch.setattr(sweep, "_RESULTS_DIR", results)

    # The conftest pins data_home() to a tmp box root and clears PROTOAGENT_* — so the
    # source and both arms resolve to box_root/<instance>, isolated from each other.
    source_root = sweep._instance_root("source")
    _make_source(source_root)
    before = _snapshot(source_root)

    _fake_sweep_boundary(monkeypatch)
    rc = evals_ab.main(
        ["--ab-learned-from", "source", "--model", "fake/model", "--port-base", "7990", "--keep"]
    )

    return types.SimpleNamespace(
        rc=rc,
        results=results,
        source_root=source_root,
        before=before,
        box_root=source_root.parent,
    )


def _arm_root(box_root: Path, arm: str) -> Path | None:
    """The one ``eval-ab-<ts>-<arm>`` instance dir, or None if it was never created."""
    roots = list(box_root.glob(f"eval-ab-*-{arm}"))
    assert len(roots) <= 1, f"expected at most one arm-{arm} dir, got {roots}"
    return roots[0] if roots else None


# ── the contract ───────────────────────────────────────────────────────────────


def test_source_stores_byte_identical_after_run(ab_run):
    """r2: the source is copied, never written — same content AND mtime, same file set."""
    assert ab_run.rc == 0
    assert _snapshot(ab_run.source_root) == ab_run.before


def test_arm_b_is_a_real_copy_not_a_symlink(ab_run):
    """r2: arm B boots on a real copy of every learned store — no symlink back."""
    b = _arm_root(ab_run.box_root, "B")
    assert b is not None, "arm B instance dir was not seeded"

    kb = b / "knowledge" / "agent.db"
    assert kb.is_file() and not kb.is_symlink()
    # A functional copy, not an empty placeholder: the row survives the backup API.
    conn = sqlite3.connect(str(kb))
    try:
        assert conn.execute("SELECT note FROM memories").fetchone()[0] == "learned: user prefers tabs"
    finally:
        conn.close()

    for rel in ("memory/sess-1.json", "skills.db", "skills/deploy/SKILL.md"):
        assert (b / rel).is_file(), f"arm B missing copied store {rel}"
    # Nothing under B is a symlink — the whole seed tree is real files.
    assert not any(p.is_symlink() for p in b.rglob("*"))


def test_arm_a_has_no_learned_memories_or_skills(ab_run):
    """r3: arm A is the bundled template only — none of the source's learned stores."""
    a = _arm_root(ab_run.box_root, "A")
    base = a if a is not None else ab_run.box_root / "eval-ab-missing-A"
    for rel in ("knowledge/agent.db", "skills.db", "memory", "skills"):
        assert not (base / rel).exists(), f"arm A unexpectedly carries {rel}"


def test_both_arm_reports_are_written(ab_run):
    """r1: each arm's normal report lands in the results dir, arm obvious in the name."""
    a_reports = list(ab_run.results.glob("ab-A-*.json"))
    b_reports = list(ab_run.results.glob("ab-B-*.json"))
    assert len(a_reports) == 1 and len(b_reports) == 1
    for rep in (*a_reports, *b_reports):
        payload = json.loads(rep.read_text())
        assert "results" in payload and payload["total"] >= 1


def test_source_by_directory_is_copied_and_left_untouched(tmp_path, monkeypatch):
    """--ab-learned-from-dir names a desktop member's root directly; same copy contract."""
    results = tmp_path / "results"
    monkeypatch.setattr(sweep, "_RESULTS_DIR", results)
    source_root = tmp_path / "workspaces" / "member-1234"
    _make_source(source_root)
    before = _snapshot(source_root)
    _fake_sweep_boundary(monkeypatch)

    rc = evals_ab.main(["--ab-learned-from-dir", str(source_root), "--model", "fake/model", "--keep"])

    assert rc == 0
    assert _snapshot(source_root) == before
    arm_b = _arm_root(sweep._instance_root("x").parent, "B")
    assert arm_b is not None and (arm_b / "knowledge" / "agent.db").is_file()


def test_source_id_and_directory_are_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        evals_ab.main(["--ab-learned-from", "a", "--ab-learned-from-dir", str(tmp_path)])


def test_arm_reports_are_labelled_with_the_model(tmp_path, monkeypatch):
    """The label names the model (as evals.sweep does), not just the arm, so the
    comparison headline and evals.report trends show what was measured."""
    monkeypatch.setattr(sweep, "_RESULTS_DIR", tmp_path / "results")
    _make_source(sweep._instance_root("source"))
    labels = []

    def fake_run_one_model(model, **kwargs):
        labels.append(kwargs["label"])
        return [{"model": kwargs["label"], "total": 0, "results": []}]

    monkeypatch.setattr(sweep, "_run_one_model", fake_run_one_model)
    evals_ab.main(["--ab-learned-from", "source", "--model", "fake/model"])
    assert labels == ["fake/model @ ab-A", "fake/model @ ab-B"]
