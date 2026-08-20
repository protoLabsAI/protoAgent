"""Project onboarding — the ``onboard_project`` tool (#2555).

Clone a GitHub repo and register it as a managed project (ADR 0095), but only
INSIDE the space the operator consented to. The bounds are the whole point:

- ``onboarding.allow`` — glob allowlist (same ``fnmatch`` semantics as
  ``plugins.sources.allow``) matched against the canonical ``github.com/owner/repo``.
  Empty = nothing is allowed (opt-in: the operator declares what may be cloned).
- ``onboarding.root`` — every checkout lands here and every registration must
  RESOLVE under it. A ``../`` or a symlink that would escape the root is refused
  before anything is written.
- ``onboarding.enabled`` — off by default. When off the tool is ABSENT from the
  toolset entirely (the factory returns ``[]``), not present-but-refusing.

The factory closes over the live ``LangGraphConfig`` (the ``config_tools.py``
precedent) so the tool reads the resolved onboarding config the graph was built
from, not a re-read of disk. Registration goes through the injected
``HOST.apply_settings`` seam (``graph.plugins.host``), never a ``server`` import —
``tools/`` sits under the import-layering contract that forbids one.

**Where a registration lands (ADR 0095).** The top-level ``projects:`` registry is
the one place a project is declared; ``filesystem.projects`` (the fs fence) and the
github plugin's ``repos`` picker are PROJECTIONS of it, and an explicitly configured
projection wins over the derived one. So the tool writes the registry entry
(``name`` / ``path`` / ``github`` / ``default_branch`` / ``write``) — that is what
makes the repo reachable by every registry consumer, the github plugin included —
and, only when this instance still carries an explicit ``filesystem.projects``
override, appends the fence projection there too, because otherwise the override
would shadow the new entry out of the fence. (Before this the tool wrote ONLY the
fence override, which the github plugin never reads: an onboarded repo was invisible
to ``/issue`` and the GitHub board no matter what.)
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import subprocess
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.tools.onboard")

# git clone can wedge on a bad URL or a dead network — a bounded wait keeps a
# single onboarding call from freezing the whole turn.
_CLONE_TIMEOUT_S = 120


def _parse_repo(github_repo: str) -> tuple[str, str] | None:
    """Normalize a repo reference to ``(owner, repo)``.

    Accepts ``owner/repo``, ``github.com/owner/repo``, and
    ``https://github.com/owner/repo`` (with or without a trailing ``.git``).
    Returns ``None`` when it can't be parsed into an owner + repo pair.

    ``repo`` is the remainder after the first ``/`` — deliberately NOT split
    further, so a traversal like ``owner/../../etc`` survives parsing and is
    caught by the root check downstream instead of being silently rewritten.
    """
    raw = (github_repo or "").strip()
    if not raw:
        return None
    # Strip a scheme, then the host, then a .git suffix and any trailing slashes.
    for scheme in ("https://", "http://", "git://", "ssh://"):
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme) :]
            break
    if raw.lower().startswith("git@github.com:"):
        raw = raw[len("git@github.com:") :]
    if raw.lower().startswith("github.com/"):
        raw = raw[len("github.com/") :]
    raw = raw.strip("/")
    if raw.lower().endswith(".git"):
        raw = raw[: -len(".git")]
    owner, _, repo = raw.partition("/")
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return None
    return owner, repo


def _default_branch(checkout: Path) -> str:
    """The checkout's remote default branch (``origin/HEAD`` → ``main``), or
    ``"main"`` when git can't say (a reused directory that isn't a clone, an offline
    remote). The registry's ``default_branch`` feeds the board's worktrees/PRs, so a
    wrong guess costs a bad PR base — but a refusal to register would cost more."""
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — best-effort identity, never a failure path
        return "main"
    ref = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if ref.startswith("origin/"):
        ref = ref[len("origin/") :]
    return ref or "main"


def _same_path(a, b: Path) -> bool:
    """True when config entry path ``a`` names the same location as ``b``."""
    try:
        return Path(str(a)).expanduser().resolve() == b.resolve()
    except (OSError, ValueError, RuntimeError):
        return str(a) == str(b)


def build_onboard_tools(config) -> list:
    """Bind ``onboard_project`` against the LIVE config — or return ``[]``.

    When ``onboarding.enabled`` is False the tool is absent from the toolset
    entirely: an operator who didn't opt in gets no onboarding surface, and the
    refusal for "disabled" is that absence plus the system prompt naming the
    config key — not a tool that exists only to say no.
    """
    if not getattr(config, "onboarding_enabled", False):
        return []

    @tool
    async def onboard_project(
        github_repo: str,
        name: str | None = None,
        write: bool | None = None,
    ) -> str:
        """Clone a GitHub repo and register it as a managed project you can work in.

        Use this to onboard a repository the operator has consented to: it is
        cloned into the configured onboarding root and added to the managed-
        projects registry, which is what your filesystem tools, the GitHub
        plugin's repo picker / ``/issue``, and the project board all read — one
        registration reaches all of them. Registration is READ-ONLY unless the
        operator's default (or your ``write=true``) says otherwise.

        Args:
            github_repo: The repo to onboard — ``owner/repo``,
                ``github.com/owner/repo``, or ``https://github.com/owner/repo``.
            name: Project name for the registry. Defaults to the repo name.
            write: Register read-write (``true``) or read-only (``false``).
                Omit to use the operator's configured default.

        This is BOUNDED and will refuse rather than reach outside its bounds:

        - Outside the ``onboarding.allow`` globs → refused, naming the patterns
          it didn't match. An empty allowlist allows nothing.
        - Outside the ``onboarding.root`` (via ``../`` or a symlink escape) →
          refused, naming the root bound.
        - A failed ``git clone`` → the git error is surfaced verbatim.
        - Already checked out → the existing checkout is REUSED as-is (no
          re-clone, no fetch, no reset — your uncommitted work is safe).
        - Already registered → success with a note; onboarding is idempotent.

        Onboarding being disabled means this tool isn't available at all; if you
        can't find it, the operator has not enabled the ``onboarding`` config
        section.

        Returns a confirmation naming the project, its path, and whether it is
        read-only or read-write — or a ``Refused:``/``Error:`` string.
        """
        parsed = _parse_repo(github_repo)
        if parsed is None:
            return (
                f"Error: could not parse GitHub repo reference {github_repo!r} — expected "
                "owner/repo, github.com/owner/repo, or https://github.com/owner/repo."
            )
        owner, repo_name = parsed
        normalized = f"github.com/{owner}/{repo_name}"
        clone_url = f"https://github.com/{owner}/{repo_name}.git"

        # (1) allow globs — same fnmatch semantics as plugins.sources.allow. Empty
        #     allowlist matches nothing, so onboarding is opt-in by declaration.
        allow = list(getattr(config, "onboarding_allow", []) or [])
        if not any(fnmatch.fnmatch(normalized, pat) for pat in allow):
            return (
                f"Refused: {normalized} does not match any allowed source pattern "
                f"({', '.join(allow)})"
            )

        # (2) resolve the clone path and confirm it stays UNDER the root. resolve()
        #     collapses .. and follows symlinks, so an escape is caught here — before
        #     git or the config writer touches anything.
        root_raw = getattr(config, "onboarding_root", "") or ""
        root = Path(root_raw).expanduser()
        target = root / repo_name
        if not target.resolve().is_relative_to(root.resolve()):
            return f"Refused: {target} resolves outside the onboarding root ({root_raw})"

        # (3) clone, or reuse an existing checkout untouched. An operator may have
        #     work in progress there, so we never fetch/reset a directory we find.
        reused_checkout = target.is_dir()
        if not reused_checkout:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "clone", clone_url, str(target)],
                    capture_output=True,
                    text=True,
                    timeout=_CLONE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return (
                    f"Error: git clone timed out after {_CLONE_TIMEOUT_S}s cloning {clone_url} — "
                    "the URL may be wrong or the network is unreachable."
                )
            except Exception as exc:  # noqa: BLE001 — a tool must not raise into the turn
                log.exception("[onboard] git clone failed to run")
                return f"Error: git clone could not run: {exc}"
            if proc.returncode != 0:
                err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit code {proc.returncode}"
                return f"Error: git clone failed: {err}"

        # (4) register via read-merge-write on the ADR 0095 `projects:` registry —
        #     the one declaration every consumer projects from (fs fence, the github
        #     plugin's picker, the board). Idempotent on path. When this instance
        #     still carries an EXPLICIT `filesystem.projects` override, that override
        #     shadows the registry in the fence (D2: explicit wins), so the fence
        #     projection is appended there as well — otherwise the repo would be
        #     registered yet unreachable by the fs tools. Never the other way round:
        #     the fence entry alone was the pre-#2925 behavior, and the github plugin
        #     reads the registry, not the fence.
        write_effective = write if write is not None else bool(getattr(config, "onboarding_write_default", False))
        rw = "read-write" if write_effective else "read-only"
        project_name = name or target.name
        default_branch = await asyncio.to_thread(_default_branch, target)

        # Merge against the LIVE registry — the same ``HOST.config`` seam the fs
        # tools resolve through (#2836) — so a second onboarding in one turn sees
        # the first instead of silently dropping it from filesystem.projects.
        # Guarded exactly like ``fs_tools._RegistryRef._live_config``: a raising
        # getter (e.g. a mid-reload race) must fall back to the build-time config,
        # never crash a call that already cloned to disk — that would strand a
        # cloned-but-unregistered directory.
        from graph.plugins.host import HOST

        live_config = None
        if HOST.config is not None:
            try:
                live_config = HOST.config()
            except Exception:  # noqa: BLE001 — degrade to the build-time config, never raise into the turn
                log.warning("[onboard] live config read failed — merging against the build-time config", exc_info=True)
        source = live_config if live_config is not None else config

        registry = [e for e in (getattr(source, "projects", []) or []) if isinstance(e, dict)]
        fence = [e for e in (getattr(source, "filesystem_projects", []) or []) if isinstance(e, dict)]
        in_registry = any(_same_path(e.get("path"), target) for e in registry)
        in_fence = any(_same_path(e.get("path"), target) for e in fence)
        if in_registry and (in_fence or not fence):
            return (
                f"{project_name} is already registered at {target} ({rw}). "
                "Reused the existing checkout — nothing changed."
            )

        github = f"{owner}/{repo_name}"
        updates: dict = {}
        if not in_registry:
            updates["projects"] = registry + [
                {
                    "name": project_name,
                    "path": str(target),
                    "github": github,
                    "default_branch": default_branch,
                    "write": write_effective,
                }
            ]
        if fence and not in_fence:
            # An explicit fence override is in force — mirror the entry into it so
            # the registry write actually reaches the fs tools (D2: explicit wins).
            fence_entry = {"name": project_name, "path": str(target), "write": write_effective, "github": github}
            updates["filesystem"] = {"projects": fence + [fence_entry], "enabled": True}
        if not fence:
            # The registry IS the fence on this instance; make sure the fence is on.
            updates.setdefault("filesystem", {})["enabled"] = True

        # Belt-and-suspenders safety invariant: every list we write back must be a
        # SUPERSET of what was there — a register must never DROP a project. (The
        # POST 409 guard is the server-side net; this is the tool-side one.)
        for label, before_list, after_list in (
            ("projects", registry, updates.get("projects")),
            ("filesystem.projects", fence, (updates.get("filesystem") or {}).get("projects")),
        ):
            if after_list is None:
                continue
            before = {str(e.get("path")) for e in before_list}
            after = {str(e.get("path")) for e in after_list if isinstance(e, dict)}
            if not before <= after:
                log.error("[onboard] refusing to apply: merged %s would drop an existing entry", label)
                return (
                    "Error: internal safety check failed — registration would have dropped an existing "
                    "project; aborted."
                )

        # Apply through the injected HOST seam (server wires HOST.apply_settings to
        # _apply_settings_changes) — tools/ must never import server/ (import-linter),
        # the same reason HOST.publish / reload_callback exist. Heavy (a full reload),
        # so off the event loop.
        if HOST.apply_settings is None:
            return (
                f"Error: cloned to {target} but registration is unavailable — the host is not wired "
                "for config apply (no running server)."
            )
        ok, messages = await asyncio.to_thread(HOST.apply_settings, updates)
        if not ok:
            return f"Error: cloned to {target} but registration failed: {'; '.join(messages) or 'unknown error'}"

        if in_fence and not in_registry:
            verb = "Promoted the existing filesystem.projects entry for"
        elif reused_checkout:
            verb = "Reused existing checkout and registered"
        else:
            verb = "Cloned and registered"
        where = "the managed-projects registry" + (
            " + the explicit filesystem.projects override" if fence and not in_fence else ""
        )
        return (
            f"{verb} {project_name} ({rw}) at {target} in {where} — GitHub {github}, "
            f"default branch {default_branch}. The GitHub plugin's repo picker and the project board "
            "read that registry; no further registration is needed."
        )

    return [onboard_project]
