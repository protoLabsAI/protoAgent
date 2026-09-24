"""Secret-like path deny-list for the console code pane (ADR 0112).

The code pane (``GET /api/fs/file``, ``GET /api/fs/diff``) and the ``show_code`` agent
tool put file CONTENT in front of whoever is looking at the console — a screen share,
a tailnet peer, a screenshot pasted into an issue. A file whose name says "credential"
must not render there just because an agent pointed at it or a diff touched it, so
those surfaces ask :func:`is_secret_path` first and show a locked placeholder instead.

Scope is deliberately narrow: this module is a NAME-based policy for display surfaces.
It does not change what ``read_file`` can read (ADR 0112 names that as a follow-up), and
it is not a content scanner — a key pasted into ``notes.txt`` is not caught here.

Matching is on path COMPONENTS, case-insensitive, with ``\\`` and ``/`` both treated as
separators, so ``config/.ENV``, ``a\\b\\id_rsa`` and ``home/.ssh/config`` all match.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePath

# `.env.*` variants that are, by near-universal convention, committed TEMPLATES with
# placeholder values — denying them would hide exactly the file an operator reads to
# learn which variables a project wants.
_ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template"})

# Exact basenames (lower-cased).
_EXACT_NAMES = {
    "secrets.yaml": "a secrets file",
    "secrets.yml": "a secrets file",
    ".netrc": "a credentials file (.netrc)",
    ".npmrc": "a registry credentials file (.npmrc)",
    ".pypirc": "a registry credentials file (.pypirc)",
}

# Basename globs (lower-cased), checked in order; first hit names the reason.
_NAME_GLOBS = (
    ("*.pem", "key/certificate material (*.pem)"),
    ("*.key", "key material (*.key)"),
    ("*.p12", "a PKCS#12 bundle (*.p12)"),
    ("*.pfx", "a PKCS#12 bundle (*.pfx)"),
    ("*.keystore", "a keystore (*.keystore)"),
    ("id_rsa*", "an SSH key (id_rsa*)"),
    ("id_ed25519*", "an SSH key (id_ed25519*)"),
    ("credentials*.json", "a credentials file (credentials*.json)"),
)


def _parts(rel: str | PurePath) -> list[str]:
    text = str(rel).replace("\\", "/")
    return [p for p in text.split("/") if p not in ("", ".")]


def is_secret_path(rel: str | PurePath) -> str | None:
    """The reason ``rel`` looks like a secret, or ``None`` when it doesn't.

    ``rel`` is a project-relative path (an absolute one works too — only its
    components are inspected). Callers that follow symlinks should ALSO check the
    resolved target's relative path: ``notes.txt -> .env`` is a secret by content.
    """
    parts = [p.casefold() for p in _parts(rel)]
    if not parts:
        return None
    # Anything under an .ssh directory (keys, known_hosts, config) — the directory
    # itself included, so a listing/diff entry for it is denied as well.
    if ".ssh" in parts:
        return "inside an .ssh directory"
    name = parts[-1]
    if name == ".env":
        return "an environment file (.env)"
    if name.startswith(".env."):
        if name[len(".env.") :] in _ENV_TEMPLATE_SUFFIXES:
            return None
        return "an environment file (.env.*)"
    if name in _EXACT_NAMES:
        return _EXACT_NAMES[name]
    for pattern, reason in _NAME_GLOBS:
        # fnmatchCASE on the already-folded name: plain fnmatch would re-fold per OS.
        if fnmatch.fnmatchcase(name, pattern):
            return reason
    return None
