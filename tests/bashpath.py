"""Find a REAL bash for tests that exercise the repo's shell scripts.

On Windows, ``shutil.which("bash")`` happily returns
``C:\\Windows\\System32\\bash.exe`` — the WSL *stub*, which prints a UTF-16
"no installed distributions" banner and exits 1. Git for Windows ships the
bash these tests actually need; derive it from git's own location
(``…\\Git\\cmd\\git.exe`` → ``…\\Git\\bin\\bash.exe``). #2412 phase 5.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def real_bash() -> str | None:
    """Path to a usable bash, or None (callers skip). POSIX: plain ``bash``."""
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        candidate = Path(git).parent.parent / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    bash = shutil.which("bash")
    # Never hand back the WSL stub — a test running it gets the distro banner.
    if bash and "system32" not in bash.lower():
        return bash
    return None
