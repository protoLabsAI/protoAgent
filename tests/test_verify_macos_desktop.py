"""Regression checks for the macOS release-artifact verifier."""

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify-macos-desktop.sh"


def test_signature_probe_does_not_pipe_codesign_into_grep_q() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'SIGNATURE_INFO="$(codesign -dvv "$APP" 2>&1 || true)"' in source
    assert '[[ "$SIGNATURE_INFO" != *"Authority=Developer ID Application:"* ]]' in source
    assert 'codesign -dvv "$APP" 2>&1 | grep -q' not in source
