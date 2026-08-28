"""The lock-downgrade gate: it has to catch #3218 and stay quiet otherwise.

A guard that only ever passes is indistinguishable from no guard, so these
assert both directions — including against the real commit that motivated it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lock_diff.py"


def _lock(**versions: str) -> str:
    return "\n".join(
        textwrap.dedent(f"""
            [[package]]
            name = "{name}"
            version = "{ver}"
        """).strip()
        for name, ver in versions.items()
    )


def _run(tmp_path: Path, base: dict[str, str], head: dict[str, str], *extra: str):
    """Run the script against a throwaway git repo whose HEAD lock is `head`."""
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "lock_diff.py").write_text(SCRIPT.read_text())
    (tmp_path / "uv.lock").write_text(_lock(**base))
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (tmp_path / "uv.lock").write_text(_lock(**head))
    return subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "lock_diff.py"), "--base", "HEAD", *extra],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_a_downgrade_fails(tmp_path):
    r = _run(tmp_path, {"langgraph": "1.2.11"}, {"langgraph": "1.2.2"})
    assert r.returncode == 1
    assert "DOWNGRADES" in r.stderr
    assert "langgraph" in r.stderr


def test_the_3218_shape_fails_naming_every_downgrade(tmp_path):
    """One package up, three quietly down — the exact shape that got merged."""
    base = {"websockets": "15.0.1", "langchain": "1.3.18", "langgraph": "1.2.11", "langgraph-sdk": "0.4.2"}
    head = {"websockets": "17.0.1", "langchain": "1.3.2", "langgraph": "1.2.2", "langgraph-sdk": "0.3.15"}
    r = _run(tmp_path, base, head)
    assert r.returncode == 1
    for name in ("langchain", "langgraph", "langgraph-sdk"):
        assert name in r.stderr
    assert "websockets" not in r.stderr  # the upgrade is not what failed


def test_upgrades_alone_pass(tmp_path):
    r = _run(tmp_path, {"fastapi": "0.136.3"}, {"fastapi": "0.141.1"})
    assert r.returncode == 0, r.stderr


def test_removals_and_additions_never_fail(tmp_path):
    """ddgs 9.16 dropping brotli/h2/socksio is routine; gating on it would train
    people to ignore this check."""
    r = _run(tmp_path, {"ddgs": "9.14.4", "h2": "4.3.0"}, {"ddgs": "9.16.0", "primp": "2.0.0"})
    assert r.returncode == 0, r.stderr
    assert "removed" in r.stdout and "added" in r.stdout


def test_the_escape_hatch_reports_without_failing(tmp_path):
    r = _run(tmp_path, {"langgraph": "1.2.11"}, {"langgraph": "1.2.2"}, "--allow-downgrades")
    assert r.returncode == 0
    assert "DOWNGRADED" in r.stdout


def test_an_unchanged_lock_says_so(tmp_path):
    r = _run(tmp_path, {"fastapi": "0.141.1"}, {"fastapi": "0.141.1"})
    assert r.returncode == 0
    assert "unchanged" in r.stdout


@pytest.mark.parametrize(
    ("before", "after", "is_down"),
    [
        ("1.2.11", "1.2.2", True),      # the naive string compare gets this wrong
        ("1.10.0", "1.9.0", True),
        ("2026.5.9", "2026.7.19", False),
        ("0.4.2", "0.3.15", True),
        ("1.0.0", "1.0.0.post1", False),
    ],
)
def test_version_ordering(before, after, is_down):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from lock_diff import _is_downgrade
    finally:
        sys.path.pop(0)
    assert _is_downgrade(before, after) is is_down
