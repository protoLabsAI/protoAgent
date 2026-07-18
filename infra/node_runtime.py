"""Managed Node.js runtime discovery + PATH wiring (ADR 0085).

protoAgent's npm-ecosystem extensions need ``node``/``npx`` on PATH: the ACP
coding agents launched via ``npx`` (Claude Code, Codex) and the many
``npx``-based MCP servers. A desktop user who never installed Node has neither,
so those features dead-end with *"agent binary not found: 'npx'"* — the existing
PATH-discovery machinery (``acp_client._discovered_node_dirs`` on the Python side,
``augmented_sidecar_path`` in the Tauri shell) can only *find* a Node the user
already has; it can't conjure one that isn't installed.

The "provision on demand" answer (``runtime.node_install``) downloads a pinned
Node into the box-shared data dir. This module is the light, dependency-free half
consumed everywhere else: where that runtime lives, and the single seam that makes
it visible to every subprocess the server spawns —
``augment_path_with_managed_node()``, called once at boot (``server.agent_init``)
and again right after an install so a live server hot-adopts it without a restart.

Kept in ``infra`` (a leaf every layer may import) with no heavy deps, so the ACP
client, the MCP launcher, the server bootstrap, and the CLI all share ONE notion
of the managed runtime — they can never disagree about where it is.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# The managed install is extracted to a stable ``current/`` dir (a real directory,
# not a symlink — Windows symlink creation needs elevation) so consumers resolve
# the bin dir without knowing the pinned version.
_CURRENT = "current"


def managed_node_root() -> Path:
    """The box-shared dir holding provisioned Node runtimes (``box_root/runtime/node``).

    Box-tier, not instance-tier: one machine provisions Node once and every instance
    (the default, the dev sandbox, every fleet member) shares it. A per-instance copy
    would waste ~150 MB each and re-download on every ``dev-reset``."""
    from infra.paths import instance_paths

    return instance_paths().box_root / "runtime" / "node"


def managed_node_install_dir() -> Path:
    """The stable path a provisioned Node is extracted into (``…/runtime/node/current``)."""
    return managed_node_root() / _CURRENT


def _bin_dir_for(install_dir: Path) -> Path:
    """The dir holding the ``node`` executable inside an extracted distribution:
    ``<root>/bin`` on POSIX tarballs, the distribution root itself on the Windows zip
    (which puts ``node.exe``/``npx.cmd`` at the top level)."""
    return install_dir if os.name == "nt" else install_dir / "bin"


def _node_exe_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def managed_node_bin_dir() -> Path | None:
    """The bin dir of a *working* managed Node install, or None.

    "Working" means the expected ``node`` executable actually exists — a
    half-extracted, wiped, or never-installed runtime returns None so callers fall
    back cleanly instead of adding a dead dir to PATH."""
    bin_dir = _bin_dir_for(managed_node_install_dir())
    return bin_dir if (bin_dir / _node_exe_name()).exists() else None


def node_on_path(path: str | None = None) -> str | None:
    """Absolute path to a ``node`` already resolvable on ``path`` (default: the process
    PATH), or None. This is the "user already has Node" check — when it's truthy we
    never touch PATH; a user's own install always wins.

    Called positionally for the default (``shutil.which`` already falls back to the
    process PATH) so the common path stays a plain one-arg call."""
    return shutil.which("node") if path is None else shutil.which("node", path=path)


def augment_path_with_managed_node() -> str | None:
    """Make the managed Node visible to every subprocess this process spawns, by
    APPENDING its bin dir to ``os.environ['PATH']`` — but only when Node isn't already
    resolvable (a user's own install wins) and a managed install actually exists.

    Idempotent: a no-op once Node resolves or the dir is already on PATH. Returns the
    dir appended, or None when nothing changed.

    Append, not prepend: an explicitly-configured PATH keeps priority for everything it
    already provides; we only fill the gap. Mutating the *process* env once (rather than
    each launch site) means the ACP client, the MCP launcher, delegates, and ``gh``/git
    all inherit the runtime uniformly — one seam, not one patch per spawn call."""
    if node_on_path() is not None:
        return None
    bin_dir = managed_node_bin_dir()
    if bin_dir is None:
        return None
    added = str(bin_dir)
    path = os.environ.get("PATH") or os.defpath
    if added in path.split(os.pathsep):
        return None
    os.environ["PATH"] = os.pathsep.join([path, added]) if path else added
    return added
