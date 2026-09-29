"""Build a git fixture repo ONCE, hand each test a private copy.

Spawning ``git`` is the dominant cost of repo-shaped fixtures (and process spawn is
especially slow on the Windows CI runners). A test that only needs "a repo in state X"
doesn't need to re-run the ``init``/``commit``/``push`` recipe itself: build the state
once per module/session with a ``tmp_path_factory`` template, then ``copytree`` it into
the test's own ``tmp_path``. Every test still gets an independent, fully real repo —
mutations never leak between tests — at the cost of a directory copy instead of ~5-10
subprocesses.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


def copy_repo(template: Path, dest: Path) -> Path:
    """Copy a standalone repo (no remotes) to ``dest`` and return ``dest``."""
    shutil.copytree(template, dest, symlinks=True)
    return dest


_URL_LINE = re.compile(r"^(\s*url\s*=\s*).*$", re.MULTILINE)


def point_remote_at(clone: Path, remote_url: Path, *, remote: str = "origin") -> None:
    """Rewrite ``remote``'s URL in ``clone``'s ``.git/config`` without spawning git.

    A copied clone still points at the TEMPLATE's origin, so pushes would land in (and
    leak through) the shared template. The URL is written quoted with forward slashes,
    which git accepts on every platform and needs no config-escaping for Windows paths.
    """
    config = clone / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    header = f'[remote "{remote}"]'
    start = text.index(header)
    end = text.find("\n[", start + len(header))
    end = len(text) if end == -1 else end
    section, n = _URL_LINE.subn(lambda m: f'{m.group(1)}"{remote_url.as_posix()}"', text[start:end], count=1)
    assert n == 1, f"no url line under {header} in {config}"
    config.write_text(text[:start] + section + text[end:], encoding="utf-8")


def copy_clone_with_origin(
    template_root: Path, dest_root: Path, *, origin: str = "origin.git", work: str = "work"
) -> Path:
    """Copy a ``<root>/{origin.git, work}`` bare-origin + clone pair into ``dest_root``
    and re-point the copied clone at the copied origin. Returns the copied work clone."""
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_root / origin, dest_root / origin, symlinks=True)
    clone = copy_repo(template_root / work, dest_root / work)
    point_remote_at(clone, dest_root / origin)
    return clone
