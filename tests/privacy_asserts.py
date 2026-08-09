"""The portable owner-only assertion for credential files (#2412 phase 4).

POSIX has one word for the contract: mode ``0600``. Windows mode bits are
decorative (``stat`` reports 0666 regardless), so the contract there is
ACL-shaped: **no broad principal may hold access** — neither via a direct
grant nor via inheritance. That is satisfied two ways, both legitimate:

- the hardened state ``infra.paths.harden_private_file`` produces
  (inheritance stripped, a single owner grant — the OpenSSH-for-Windows
  private-key posture), or
- inheritance from a private per-user directory (instance dirs live under
  the user profile), which is how e.g. a rollback-restored file stays
  private without re-hardening.
"""

from __future__ import annotations

import os
import stat
import subprocess

#: Principals that must never appear in a credential file's ACL. Substring
#: match against icacls output lines, case-insensitive.
_BROAD_PRINCIPALS = (
    "everyone",
    "authenticated users",
    "builtin\\users",
    r"nt authority\interactive",
)


def assert_owner_only(path) -> None:
    """Assert ``path`` honors the credential-privacy contract, portably."""
    path = str(path)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, (
            f"{path} mode is {oct(stat.S_IMODE(os.stat(path).st_mode))}, want 0o600"
        )
        return
    out = subprocess.run(["icacls", path], capture_output=True, text=True, timeout=10).stdout
    ace_lines = [ln for ln in out.splitlines() if ":(" in ln]
    assert ace_lines, f"icacls returned no ACEs for {path}:\n{out}"
    lowered = "\n".join(ace_lines).lower()
    for principal in _BROAD_PRINCIPALS:
        assert principal not in lowered, f"{path} grants access to broad principal {principal!r}:\n{out}"
