"""verify-macos-desktop.sh must never pipe a command into `grep -q` (QA panel finding on #4865).

Under `set -o pipefail`, `cmd | grep -q PATTERN` fails spuriously: grep exits at the first match,
the writer gets SIGPIPE, and pipefail reports the pipeline as failed, so a correctly signed
Mach-O reads as "not Developer ID-signed". The script captures output and matches in-shell."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-macos-desktop.sh"


def test_no_command_is_piped_into_grep_q():
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln.strip() for ln in code if re.search(r"\|\s*grep\s+-q", ln)]
    assert offenders == []
