"""The write fence for captured files — screenshots and PDFs.

The manifest has always declared ``filesystem: scoped``, but nothing enforced it:
``browser_screenshot(path)`` handed the path straight to the CLI, so
``browser_screenshot("~/.ssh/authorized_keys")`` would have Chrome overwrite it, and a
prompt-injected page ("save a screenshot to …") could pick the target. #3451 makes the
declaration true: every capture path is resolved INSIDE this plugin's own instance store
and an escape is **refused**, not silently redirected — writing an operator's data
somewhere unexpected is worse than failing loudly (the same rule
``graph/sdk.plugin_store`` applies to its ``subdir``).

The root is the host's plugin store (``sdk.plugin_store(plugin_id="agent_browser")`` →
``<instance_root>/agent_browser/captures``), so it is per-instance (ADR 0004: the dev
sandbox writes to its own root, never the default instance's) and it lands where the rest
of this plugin's state would. ``protoagent config explain`` prints the instance root, so
an operator can always find the files.

Containment is checked AFTER ``Path.resolve()``, which normalizes ``..`` and follows
symlinks — so ``../../etc/x``, an absolute path, and a symlink inside the root pointing
out are all caught by the same check.
"""

from __future__ import annotations

from pathlib import Path

CAPTURE_SUBDIR = "captures"
PLUGIN_ID = "agent_browser"


def capture_root() -> Path:
    """The only directory the capture tools may write to (created on demand)."""
    from graph import sdk

    return sdk.plugin_store(CAPTURE_SUBDIR, plugin_id=PLUGIN_ID)


def resolve_capture_path(path: str | None, *, default_name: str) -> Path:
    """A capture path fenced to :func:`capture_root`, or ``ValueError`` if it escapes.

    * blank ``path`` → ``<root>/<default_name>``;
    * a relative path (``shots/home.png``) → under the root, parents created;
    * an absolute path → accepted ONLY if it already resolves inside the root (so an
      agent can re-use a path a previous call returned), refused otherwise.
    """
    root = capture_root().resolve()
    raw = str(path or "").strip() or default_name
    candidate = Path(raw).expanduser()
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(
            f"refusing to write outside the plugin's capture directory: pass a name or a "
            f"relative path (e.g. {default_name!r}) — files land in {root}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
