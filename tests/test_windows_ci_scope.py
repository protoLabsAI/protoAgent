"""Regression coverage for the fail-safe Windows CI change classifier."""

import io
import json
import sys
from pathlib import Path

from scripts.windows_ci_scope import WindowsScope, classify_paths, main


ROOT = Path(__file__).parent.parent


def test_docs_web_and_marketing_changes_skip_native_suites() -> None:
    """Known platform-neutral surfaces should not allocate Windows runners."""

    assert classify_paths(
        ["docs/adr/0104-session-turns-read-api.md", "apps/web/src/app/App.tsx", "sites/marketing/index.html"]
    ) == WindowsScope(python_tests=False, rust_tests=False)


def test_python_or_unknown_changes_fail_safe_to_python_suite() -> None:
    """Runtime, dependency, and unfamiliar paths retain Windows Python coverage."""

    for path in ("graph/agent.py", "requirements.txt", "uv.lock", "plugins/example/config.yaml"):
        assert classify_paths([path]) == WindowsScope(python_tests=True, rust_tests=False)


def test_desktop_rust_change_runs_rust_without_unrelated_python() -> None:
    """A Tauri-only change gets the native gate that exercises its actual code."""

    assert classify_paths(["apps/desktop/src-tauri/src/lib.rs"]) == WindowsScope(
        python_tests=False,
        rust_tests=True,
    )


def test_desktop_sidecar_change_keeps_windows_python_coverage() -> None:
    """The frozen Python sidecar remains part of the Windows Python surface."""

    assert classify_paths(["apps/desktop/sidecar/build_sidecar.py"]) == WindowsScope(
        python_tests=True,
        rust_tests=False,
    )


def test_classifier_and_workflow_changes_run_every_native_gate() -> None:
    """Changes to the gate itself must validate both branches of its contract."""

    for path in (".github/workflows/checks.yml", "scripts/windows_ci_scope.py", "tests/test_windows_ci_scope.py"):
        assert classify_paths([path]) == WindowsScope(python_tests=True, rust_tests=True)


def test_empty_or_unreadable_change_set_fails_closed() -> None:
    """Missing diff input must cost time rather than silently remove coverage."""

    assert classify_paths([]) == WindowsScope(python_tests=True, rust_tests=True)


def test_mixed_change_set_takes_union_of_required_gates() -> None:
    """A cross-stack change runs each native suite implicated by any path."""

    assert classify_paths(["server/cli.py", "apps/desktop/src-tauri/src/lib.rs"]) == WindowsScope(
        python_tests=True,
        rust_tests=True,
    )


def test_cli_unions_positional_and_stdin_paths(monkeypatch, capsys) -> None:
    """Mixed input modes must not discard a gate-requiring positional path."""

    stdin = io.TextIOWrapper(io.BytesIO(b"docs/guide.md\0"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)

    assert main(["server/cli.py", "--stdin0"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "python_tests": True,
        "rust_tests": False,
    }


def test_cli_json_uses_booleans(capsys) -> None:
    """Diagnostic JSON should remain type-safe for non-Actions consumers."""

    assert main(["docs/guide.md"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "python_tests": False,
        "rust_tests": False,
    }


def test_checks_workflow_preserves_stable_gate_and_full_suite_shards() -> None:
    """Source guard the stable required check and coverage-preserving matrix wiring."""

    workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
    assert "\n  windows-scope:\n" in workflow
    assert "\n  windows-python-tests:\n" in workflow
    assert "group: [1, 2]" in workflow
    assert "pytest-split==0.11.0" in workflow
    assert "--splits 2 --group ${{ matrix.group }}" in workflow
    assert "--splitting-algorithm least_duration" in workflow
    assert "--durations-path tests/windows_test_durations.json" in workflow
    assert "\n  windows-rust-tests:\n" in workflow
    assert "cargo test --locked" in workflow
    assert "\n  windows-tests:\n    name: Windows tests (native)\n" in workflow
    assert "needs: [windows-scope, windows-python-tests, windows-rust-tests]" in workflow
