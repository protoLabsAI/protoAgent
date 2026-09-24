"""Project onboarding — ``onboard_project`` and ``register_local_project`` (#2555).

``onboard_project`` clones a git repo — from any host, over https or ssh — and
registers it as a managed project (ADR 0095); ``register_local_project`` registers a
directory that is already on disk. Both work only INSIDE the space the operator
consented to. The bounds are the whole point:

- ``onboarding.allow`` — glob allowlist (same ``fnmatch`` semantics as
  ``plugins.sources.allow``) matched against the canonical ``host/owner/repo`` of a
  CLONE source (``github.com/acme/widget``, ``gitlab.com/acme/tools/cli``). Empty =
  nothing may be cloned (opt-in: the operator declares what may be fetched). It does
  not gate ``register_local_project`` — a local directory has no source to match;
  the root bounds it.
- ``onboarding.root`` — every checkout lands here and every registration must
  RESOLVE under it. A ``../`` or a symlink that would escape the root is refused
  before anything is written. This is the whole fence for local registration, so
  the operator widens what can be registered by widening the root, never the agent.
- ``onboarding.enabled`` — ON by default (#3396), and a DISCOVERABILITY switch
  rather than the consent: the two bounds above carry that, and both are empty by
  default, so a stock install can onboard exactly nothing. Turning it off removes
  both tools from the toolset entirely (the factory returns ``[]``).

  It defaulted off, which was wrong for the problem #2555 set out to solve: an
  absent tool plus a settings section nobody thinks to look for left the operator
  with a dead "Add project" button and no statement of what to configure. Present
  and refusing BY NAME is the useful shape — it is how the agent tells the
  operator what to set, which is the request that started #2555 in the first place.

The factory closes over the live ``LangGraphConfig`` (the ``config_tools.py``
precedent) so the tools read the resolved onboarding config the graph was built
from, not a re-read of disk. Registration goes through the injected
``HOST.apply_settings`` seam (``graph.plugins.host``), never a ``server`` import —
``tools/`` sits under the import-layering contract that forbids one.

**Where a registration lands (ADR 0095).** The top-level ``projects:`` registry is
the one place a project is declared; ``filesystem.projects`` (the fs fence) and the
github plugin's ``repos`` picker are PROJECTIONS of it, and an explicitly configured
projection wins over the derived one. So both tools write the registry entry
(``name`` / ``path`` / ``github`` / ``default_branch`` / ``write``) through one shared
helper (``_register``) — that is what makes the repo reachable by every registry
consumer, the github plugin included — and, only when this instance still carries an
explicit ``filesystem.projects`` override, append the fence projection there too,
because otherwise the override would shadow the new entry out of the fence. (Before
#2925 the tool wrote ONLY the fence override, which the github plugin never reads: an
onboarded repo was invisible to ``/issue`` and the GitHub board no matter what.)

**Clone sources.** Any ``https``/``http``/``ssh``/``git`` URL, the scp form
``git@host:owner/repo``, ``host/owner/repo``, or a bare ``owner/repo`` (= GitHub).
git receives the URL exactly as the operator/agent gave it, so ssh keys and
credential helpers apply; a credential embedded in a URL is redacted from every
string this module returns or logs. Refused outright: local paths and ``file://``
(that is ``register_local_project``'s job, under the root), remote-helper transports
(``ext::`` runs an arbitrary command), and anything starting with ``-`` (git option
injection — the argv also carries ``--`` before the URL).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.tools.onboard")

# git clone can wedge on a bad URL or a dead network — a bounded wait keeps a
# single onboarding call from freezing the whole turn.
_CLONE_TIMEOUT_S = 120

# Transports git may be handed. ``file://`` / local paths are what
# ``register_local_project`` is for (bounded by the root, not the allowlist), and
# every ``<helper>::`` form is refused before this is consulted.
_ALLOWED_SCHEMES = ("https", "http", "ssh", "git")

# One path segment of a repo path. Deliberately narrow — no ``%`` escapes, no
# ``?``/``#``, no whitespace — and never ``.``/``..`` (checked separately), so the
# checkout directory named after the last segment can't traverse.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$")
# scp-like ``[user@]host:path`` — git's own rule is "no slash before the first colon".
# The host must be 2+ characters so a Windows drive (``C:/src/x``) is never read as one.
_SCP_RE = re.compile(r"^(?:([^@/:]+)@)?([^@/:]{2,}):(.+)$")
# URL userinfo (RFC 3986 unreserved + sub-delims + ``%``/``:``). Anything else —
# ``#``, ``?``, ``/``, ``\`` — could make git/curl see a DIFFERENT host than the one
# the allowlist was matched against (``https://evil.com#@github.com/a/b``).
_USERINFO_OK_RE = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:-]*$")
# ``scheme://userinfo@`` anywhere in a string (git's stderr echoes the URL).
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@")


def _redact(text: str) -> str:
    """Mask credentials in any ``scheme://userinfo@`` URL inside ``text``.

    ``user:secret@`` keeps the user and masks the secret. A lone userinfo is masked
    entirely for http(s)/git — ``https://<token>@host`` is how tokens usually ride —
    but kept for ssh, where it is the login name (``ssh://git@host``), not a secret.
    """

    def _sub(m: re.Match) -> str:
        scheme, info = m.group(1), m.group(2)
        if ":" in info:
            return f"{scheme}{info.split(':', 1)[0]}:***@"
        if scheme.lower().startswith("ssh"):
            return m.group(0)
        return f"{scheme}***@"

    return _USERINFO_RE.sub(_sub, text or "")


class RepoRefError(ValueError):
    """A clone source the tool refuses to hand to git — the message says why."""


@dataclass(frozen=True)
class RepoRef:
    """A parsed clone source.

    ``host`` is lower-cased and port-free; ``path`` is ``owner/repo`` (or a nested
    ``group/sub/repo``) without ``.git``. ``clone_url`` is what git receives — the
    caller's own URL form when one was given — and ``display_url`` is the same with
    any credential redacted, the only form that may appear in output or logs.
    """

    host: str
    path: str
    clone_url: str

    @property
    def normalized(self) -> str:
        """``host/owner/repo`` — what ``onboarding.allow`` globs are matched against."""
        return f"{self.host}/{self.path}"

    @property
    def name(self) -> str:
        """The repo's own name (last path segment) — the checkout directory."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def github_slug(self) -> str:
        """``owner/repo`` for a github.com source, else ``""`` (the registry's
        ``github`` binding is GitHub-only: it feeds the GitHub plugin)."""
        return self.path if self.host == "github.com" and self.path.count("/") == 1 else ""

    @property
    def display_url(self) -> str:
        return _redact(self.clone_url)


def _clean_path(path: str) -> str:
    """Validate + normalize a repo path (``owner/repo[.git]``) or raise."""
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    segments = path.split("/")
    if len(segments) < 2 or not all(segments):
        raise RepoRefError("expected an owner and a repo name (owner/repo)")
    for seg in segments:
        if seg in (".", ".."):
            raise RepoRefError("'.' and '..' path segments are not allowed")
        if not _SEGMENT_RE.match(seg):
            raise RepoRefError(f"path segment {seg!r} has characters a repo path can't contain")
    return "/".join(segments)


def _clean_host(host: str) -> str:
    """Validate a host (optionally ``:port``) and return it lower-cased, port-free."""
    name, colon, port = host.rpartition(":")
    if colon and port.isdigit():
        host = name
    if not _HOST_RE.match(host):
        raise RepoRefError(f"{host!r} is not a valid host name")
    return host.lower()


def _parse_repo(ref: str) -> RepoRef:
    """Parse a clone source into a :class:`RepoRef`, or raise :class:`RepoRefError`.

    Accepted: ``owner/repo`` (GitHub), ``host/owner/repo`` (https), any
    ``https://`` / ``http://`` / ``ssh://`` / ``git://`` URL, and the scp form
    ``[user@]host:owner/repo`` (an ssh config alias works as the host) — each with or without a trailing ``.git``.
    """
    raw = (ref or "").strip()
    if not raw:
        raise RepoRefError("no repository was given")
    if raw.startswith("-"):
        raise RepoRefError("a leading '-' would be read by git as an option")
    if "::" in raw:
        raise RepoRefError("git remote-helper transports (ext::, fd::, …) are not allowed")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        raise RepoRefError("whitespace and control characters are not allowed")
    if "\\" in raw:
        raise RepoRefError("backslashes are not allowed — local paths are registered with register_local_project")

    m = _SCHEME_RE.match(raw)
    if m:
        scheme, rest = m.group(1).lower(), m.group(2)
        if scheme == "file":
            raise RepoRefError("file:// is a local path — register a directory under the root with register_local_project")
        if scheme not in _ALLOWED_SCHEMES:
            raise RepoRefError(f"the {scheme}:// transport is not allowed (use {', '.join(_ALLOWED_SCHEMES)})")
        authority, slash, path = rest.partition("/")
        if not slash:
            raise RepoRefError("the URL has no repository path")
        userinfo, _at, hostport = authority.rpartition("@")
        if not _USERINFO_OK_RE.match(userinfo):
            raise RepoRefError("the URL's user/credential part has characters that could disguise its host")
        return RepoRef(host=_clean_host(hostport), path=_clean_path(path), clone_url=raw)

    m = _SCP_RE.match(raw)
    if m:
        # The scp form cannot carry a password, so nothing here needs redacting.
        return RepoRef(host=_clean_host(m.group(2)), path=_clean_path(m.group(3)), clone_url=raw)

    if raw.startswith(("/", ".", "~")) or ":" in raw:
        raise RepoRefError(
            "not a clone URL — local paths are registered with register_local_project; "
            "remote sources look like https://host/owner/repo or git@host:owner/repo"
        )
    segments = raw.strip("/").split("/")
    if len(segments) >= 3 and "." in segments[0]:
        host = _clean_host(segments[0])
        path = _clean_path("/".join(segments[1:]))
    else:
        # Bare owner/repo keeps its historical meaning: GitHub over https.
        host, path = "github.com", _clean_path(raw)
    return RepoRef(host=host, path=path, clone_url=f"https://{host}/{path}.git")


def _default_branch(checkout: Path) -> str:
    """The checkout's remote default branch (``origin/HEAD`` → ``main``), else its
    current branch (a local repo with no origin), else ``"main"`` when git can't say
    (not a git repo, git missing). The registry's ``default_branch`` feeds the board's
    worktrees/PRs, so a wrong guess costs a bad PR base — but a refusal to register
    would cost more."""
    for argv in (
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        ["git", "symbolic-ref", "--short", "HEAD"],
    ):
        try:
            proc = subprocess.run(argv, cwd=str(checkout), capture_output=True, text=True, timeout=10)
        except Exception:  # noqa: BLE001 — best-effort identity, never a failure path
            return "main"
        ref = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        if ref.startswith("origin/"):
            ref = ref[len("origin/") :]
        if ref:
            return ref
    return "main"


def _tracking_drift(checkout: Path) -> str:
    """Describe how a REUSED ``checkout`` diverges from its configured upstream,
    using ONLY local Git metadata — no network, no mutation (#3402).

    We never fetch/reset a reused checkout (an operator may have work in progress),
    so the compared refs are whatever is already on disk; the returned clause says
    so with "it was not fetched". The comparison is a read-only ``git rev-list``
    count against ``@{upstream}`` — the branch's configured tracking ref.

    Every path is best-effort and bounded: a directory that isn't a git checkout,
    a branch with no upstream, git being unavailable, or an unparseable count all
    resolve to a note that the drift COULD NOT be determined, rather than inventing
    a number. Returns a single sentence to append to the reuse message.
    """

    def _git(*argv: str):
        return subprocess.run(
            ["git", *argv],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=10,
        )

    # (a) Resolve the tracking branch (e.g. ``origin/main``). Read-only; failure
    #     here means no upstream, not a git checkout, or git can't run.
    try:
        up = _git("rev-parse", "--abbrev-ref", "@{upstream}")
    except Exception:  # noqa: BLE001 — git missing / cannot spawn; never a failure path
        return "Tracking drift could not be determined — git was unavailable; it was not fetched."
    if up.returncode != 0:
        detail = (up.stderr or "").lower()
        if "not a git repository" in detail or "not a git repo" in detail:
            return "Tracking drift could not be determined — not a git checkout; it was not fetched."
        if "no upstream" in detail:
            return (
                "Tracking drift was not checked — the current branch has no upstream "
                "tracking branch; it was not fetched."
            )
        return (
            "Tracking drift could not be determined — the tracking branch could not be "
            "resolved; it was not fetched."
        )
    upstream = (up.stdout or "").strip()
    if not upstream:
        return (
            "Tracking drift was not checked — the current branch has no upstream "
            "tracking branch; it was not fetched."
        )

    # (b) Count ahead/behind against that ref. ``--left-right --count A...HEAD``
    #     emits "<left> <right>": left = commits in the upstream not in HEAD
    #     (behind), right = commits in HEAD not in the upstream (ahead). Purely a
    #     read of local objects — no fetch, no working-tree change.
    try:
        counts = _git("rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    except Exception:  # noqa: BLE001
        return f"Tracking drift vs {upstream} could not be determined — git was unavailable; it was not fetched."
    if counts.returncode != 0:
        return f"Tracking drift vs {upstream} could not be determined; it was not fetched."
    fields = (counts.stdout or "").split()
    if len(fields) != 2 or not all(f.lstrip("-").isdigit() for f in fields):
        return f"Tracking drift vs {upstream} could not be determined; it was not fetched."
    behind, ahead = int(fields[0]), int(fields[1])

    if behind == 0 and ahead == 0:
        return f"It is up to date with {upstream}; it was not fetched."
    parts: list[str] = []
    if ahead:
        parts.append(f"{ahead} commit{'' if ahead == 1 else 's'} ahead of")
    if behind:
        parts.append(f"{behind} commit{'' if behind == 1 else 's'} behind")
    return f"It is {' and '.join(parts)} {upstream}; it was not fetched."


def _same_path(a, b: Path) -> bool:
    """True when config entry path ``a`` names the same location as ``b``."""
    try:
        return Path(str(a)).expanduser().resolve() == b.resolve()
    except (OSError, ValueError, RuntimeError):
        return str(a) == str(b)


@dataclass(frozen=True)
class _Registration:
    """Outcome of :func:`_register`. ``status`` is ``already`` (nothing written),
    ``registered`` (a new registry entry), ``promoted`` (a fence-only entry gained its
    registry entry) or ``error`` (``error`` holds a clause naming what failed).
    ``mirrored`` = the explicit ``filesystem.projects`` override was appended to."""

    status: str
    mirrored: bool = False
    error: str = ""


async def _register(
    config,
    *,
    target: Path,
    project_name: str,
    write: bool,
    github: str,
    default_branch: str,
) -> _Registration:
    """Register ``target`` in the ADR 0095 ``projects:`` registry — the ONE write path
    both onboarding tools share.

    Read-merge-write against the LIVE registry, idempotent on path. When this instance
    still carries an EXPLICIT ``filesystem.projects`` override, that override shadows
    the registry in the fence (D2: explicit wins), so the fence projection is appended
    there as well — otherwise the repo would be registered yet unreachable by the fs
    tools. Never the other way round: the fence entry alone was the pre-#2925
    behavior, and the github plugin reads the registry, not the fence. ``github`` is
    omitted from the entries when empty (a non-GitHub source or a local directory).
    """
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
        return _Registration("already")

    updates: dict = {}
    if not in_registry:
        entry: dict = {"name": project_name, "path": str(target)}
        if github:
            entry["github"] = github
        entry["default_branch"] = default_branch
        entry["write"] = write
        updates["projects"] = registry + [entry]
    if fence and not in_fence:
        # An explicit fence override is in force — mirror the entry into it so
        # the registry write actually reaches the fs tools (D2: explicit wins).
        fence_entry: dict = {"name": project_name, "path": str(target), "write": write}
        if github:
            fence_entry["github"] = github
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
            return _Registration(
                "error",
                error="registration was aborted by an internal safety check — it would have dropped an existing project",
            )

    # Apply through the injected HOST seam (server wires HOST.apply_settings to
    # _apply_settings_changes) — tools/ must never import server/ (import-linter),
    # the same reason HOST.publish / reload_callback exist. Heavy (a full reload),
    # so off the event loop.
    if HOST.apply_settings is None:
        return _Registration(
            "error", error="registration is unavailable — the host is not wired for config apply (no running server)"
        )
    ok, messages = await asyncio.to_thread(HOST.apply_settings, updates)
    if not ok:
        return _Registration("error", error=f"registration failed: {'; '.join(messages) or 'unknown error'}")
    status = "promoted" if in_fence and not in_registry else "registered"
    return _Registration(status, mirrored=bool(fence) and not in_fence)


def _where(reg: _Registration) -> str:
    return "the managed-projects registry" + (" + the explicit filesystem.projects override" if reg.mirrored else "")


_ROOT_UNSET = (
    "Refused: onboarding.root isn't set, so there is no consented space to {verb} — set "
    "Settings ▸ Capabilities ▸ Project onboarding ▸ Onboarding root first."
)


def build_onboard_tools(config) -> list:
    """Bind ``onboard_project`` + ``register_local_project`` against the LIVE config —
    or return ``[]``.

    ``onboarding.enabled`` is on by default, so the tools are normally PRESENT and
    refuse by naming the bound they hit (no allowed sources / no root). That refusal
    is the point: it is how the agent tells the operator what to configure.

    Setting it False removes both tools from the toolset entirely — for an operator
    who wants no onboarding surface at all, not as the default posture.
    """
    if not getattr(config, "onboarding_enabled", True):
        return []

    @tool
    async def onboard_project(
        repo: str = "",
        name: str | None = None,
        write: bool | None = None,
        github_repo: str = "",
    ) -> str:
        """Clone a git repository and register it as a managed project you can work in.

        Use this to onboard a repository the operator has consented to: it is
        cloned into the configured onboarding root and added to the managed-
        projects registry, which is what your filesystem tools, the GitHub
        plugin's repo picker / ``/issue``, and the project board all read — one
        registration reaches all of them. Registration is READ-ONLY unless the
        operator's default (or your ``write=true``) says otherwise. For a
        directory that is ALREADY on disk, use ``register_local_project`` instead.

        Args:
            repo: The repo to clone, from any git host: ``owner/repo`` (GitHub),
                ``gitlab.com/owner/repo``, ``https://host/owner/repo(.git)``,
                ``git@host:owner/repo(.git)`` or ``ssh://git@host/owner/repo``.
                The URL is handed to git as given, so the host's ssh keys and
                credential helpers apply.
            name: Project name for the registry. Defaults to the repo name.
            write: Register read-write (``true``) or read-only (``false``).
                Omit to use the operator's configured default.
            github_repo: Older name for ``repo`` — still accepted; prefer ``repo``.

        This is BOUNDED and will refuse rather than reach outside its bounds:

        - Outside the ``onboarding.allow`` globs (matched against
          ``host/owner/repo``, e.g. ``gitlab.com/acme/widget``) → refused, naming
          the patterns it didn't match. An empty allowlist allows nothing.
        - Outside the ``onboarding.root`` (via a symlink escape) → refused, naming
          the root bound.
        - Local paths, ``file://``, ``ext::``-style transports, and anything
          starting with ``-`` → refused before git runs.
        - A failed ``git clone`` → the git error is surfaced (credentials masked).
        - Already checked out → the existing checkout is REUSED as-is (no
          re-clone, no fetch, no reset — your uncommitted work is safe). The
          result reports how far the checkout has drifted from its tracking
          branch (e.g. "2 commits behind origin/main; it was not fetched"),
          read from local Git metadata only, or notes that it couldn't be told.
        - Already registered → success with a note; onboarding is idempotent.

        Onboarding being disabled means this tool isn't available at all; if you
        can't find it, the operator has not enabled the ``onboarding`` config
        section.

        Returns a confirmation naming the project, its path, and whether it is
        read-only or read-write — or a ``Refused:``/``Error:`` string.
        """
        given = (repo or "").strip() or (github_repo or "").strip()
        try:
            ref = _parse_repo(given)
        except RepoRefError as exc:
            return (
                f"Error: can't clone {_redact(given)!r} — {exc}. Expected owner/repo, host/owner/repo, "
                "https://host/owner/repo, or git@host:owner/repo."
            )
        normalized = ref.normalized

        # (1) allow globs — same fnmatch semantics as plugins.sources.allow. Empty
        #     allowlist matches nothing, so onboarding is opt-in by declaration.
        allow = list(getattr(config, "onboarding_allow", []) or [])
        if not allow:
            # The stock state now that `enabled` defaults on (#3396), so this is the
            # FIRST thing most operators see. "does not match any allowed source
            # pattern ()" — an empty paren — describes the state without naming the
            # remedy; say what to set and where, since being unconfigured is normal
            # here rather than a mistake.
            return (
                "Refused: no allowed clone sources are configured, so nothing can be "
                "onboarded — add a pattern under Settings ▸ Capabilities ▸ Project "
                "onboarding ▸ Allowed sources (e.g. github.com/your-org/*)."
            )
        if not any(fnmatch.fnmatch(normalized, pat) for pat in allow):
            return (
                f"Refused: {normalized} does not match any allowed source pattern "
                f"({', '.join(allow)})"
            )

        # (2) resolve the clone path and confirm it stays UNDER the root. resolve()
        #     collapses .. and follows symlinks, so an escape is caught here — before
        #     git or the config writer touches anything.
        root_raw = getattr(config, "onboarding_root", "") or ""
        # An UNSET root is not "anywhere" — it is nowhere. `Path("")` is `Path(".")`,
        # so without this an empty root silently means the server's working directory
        # and the containment check below passes trivially: the clone lands in the
        # process CWD and gets registered. Refuse and name it instead, matching the
        # board registry's wording for the same bound. (#3397 — latent before
        # #3396; reachable once `enabled` defaults on, which is what surfaced it.)
        if not root_raw.strip():
            return _ROOT_UNSET.format(verb="clone into")
        root = Path(root_raw).expanduser()
        target = root / ref.name
        if not target.resolve().is_relative_to(root.resolve()):
            return f"Refused: {target} resolves outside the onboarding root ({root_raw})"

        # (3) clone, or reuse an existing checkout untouched. An operator may have
        #     work in progress there, so we never fetch/reset a directory we find.
        #     ``--`` ends option parsing, so nothing in the URL can become a git flag
        #     (the parser already refuses a leading ``-``; this is the second net).
        #     No terminal prompt: a private https repo without credentials fails fast
        #     instead of waiting out the timeout on a password prompt nobody sees.
        reused_checkout = target.is_dir()
        if not reused_checkout:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "clone", "--", ref.clone_url, str(target)],
                    capture_output=True,
                    text=True,
                    timeout=_CLONE_TIMEOUT_S,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
            except subprocess.TimeoutExpired:
                return (
                    f"Error: git clone timed out after {_CLONE_TIMEOUT_S}s cloning {ref.display_url} — "
                    "the URL may be wrong or the network is unreachable."
                )
            except Exception as exc:  # noqa: BLE001 — a tool must not raise into the turn
                log.error("[onboard] git clone failed to run: %s", _redact(str(exc)))
                return f"Error: git clone could not run: {_redact(str(exc))}"
            if proc.returncode != 0:
                err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or f"exit code {proc.returncode}"
                return f"Error: git clone failed: {_redact(err)}"

        write_effective = write if write is not None else bool(getattr(config, "onboarding_write_default", False))
        rw = "read-write" if write_effective else "read-only"
        project_name = name or target.name
        default_branch = await asyncio.to_thread(_default_branch, target)

        # On any REUSE path we inspect local Git metadata only and report how the
        # checkout has drifted from its tracking branch (#3402). We do NOT fetch,
        # reset, or otherwise touch the checkout — the contract for a directory we
        # find is that it stays exactly as the operator left it — so the clause is
        # a read-only ahead/behind count that always states it was not fetched.
        # Only meaningful when there is a checkout on disk to compare.
        drift = ""
        if reused_checkout:
            drift = " " + await asyncio.to_thread(_tracking_drift, target)

        # (4) register — the shared read-merge-write path (see ``_register``).
        github = ref.github_slug
        reg = await _register(
            config,
            target=target,
            project_name=project_name,
            write=write_effective,
            github=github,
            default_branch=default_branch,
        )
        if reg.status == "already":
            return (
                f"{project_name} is already registered at {target} ({rw}). "
                f"Reused the existing checkout — nothing changed.{drift}"
            )
        if reg.status == "error":
            return f"Error: {'found' if reused_checkout else 'cloned to'} {target} but {reg.error}."

        if reg.status == "promoted":
            verb = "Promoted the existing filesystem.projects entry for"
        elif reused_checkout:
            verb = "Reused existing checkout and registered"
        else:
            verb = "Cloned and registered"
        if github:
            source = f"GitHub {github}"
            readers = "The GitHub plugin's repo picker and the project board read"
        else:
            source = f"source {normalized} (not GitHub, so the GitHub plugin's picker won't list it)"
            readers = "Your filesystem tools and the project board read"
        return (
            f"{verb} {project_name} ({rw}) at {target} in {_where(reg)} — {source}, "
            f"default branch {default_branch}.{drift} {readers} that registry; no further registration is needed."
        )

    @tool
    async def register_local_project(
        path: str,
        name: str | None = None,
        write: bool | None = None,
    ) -> str:
        """Register a directory that is ALREADY on disk as a managed project — no clone.

        Use this when the repo (or any project folder) already exists locally —
        e.g. a checkout the operator made, or one you created — and you need your
        filesystem tools, the GitHub plugin and the project board to reach it. It
        adds the directory to the same managed-projects registry
        ``onboard_project`` writes. If the directory's ``origin`` remote is on
        GitHub, the ``owner/repo`` binding is filled in automatically.

        Args:
            path: Absolute path to the directory (``~`` is expanded).
            name: Project name for the registry. Defaults to the directory name.
            write: Register read-write (``true``) or read-only (``false``).
                Omit to use the operator's configured default.

        BOUNDED by ``onboarding.root``: the path must RESOLVE (symlinks followed)
        to an existing directory strictly inside that root, or it is refused,
        naming the root. The root is operator-controlled — to register something
        outside it, ask the operator to change ``onboarding.root``; you cannot
        widen it. ``onboarding.allow`` does not apply (nothing is fetched).
        Already registered → success with a note; registration is idempotent.

        Returns a confirmation naming the project, its path, and whether it is
        read-only or read-write — or a ``Refused:``/``Error:`` string.
        """
        raw = (path or "").strip()
        if not raw:
            return "Error: no path was given — pass the absolute path of the directory to register."
        root_raw = getattr(config, "onboarding_root", "") or ""
        if not root_raw.strip():
            return _ROOT_UNSET.format(verb="register from")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            return f"Error: {raw!r} is not an absolute path — pass the full path (a leading ~ is fine)."
        root_resolved = Path(root_raw).expanduser().resolve()
        target = expanded.resolve()
        # Containment BEFORE existence: a path outside the root is refused the same
        # way whether or not it exists, so the tool can't be used to probe the disk.
        if not target.is_relative_to(root_resolved):
            return (
                f"Refused: {raw} resolves to {target}, outside the onboarding root ({root_raw}) — only "
                "directories under the root can be registered. The operator widens this by changing "
                "onboarding.root."
            )
        if target == root_resolved:
            return (
                f"Refused: {raw} is the onboarding root itself — register a project directory inside "
                f"it, not the whole root ({root_raw})."
            )
        if not target.is_dir():
            return f"Error: {target} is not an existing directory."

        from graph.workspaces.manager import github_slug_for_checkout

        write_effective = write if write is not None else bool(getattr(config, "onboarding_write_default", False))
        rw = "read-write" if write_effective else "read-only"
        project_name = name or target.name
        github = await asyncio.to_thread(github_slug_for_checkout, target)
        default_branch = await asyncio.to_thread(_default_branch, target)

        reg = await _register(
            config,
            target=target,
            project_name=project_name,
            write=write_effective,
            github=github,
            default_branch=default_branch,
        )
        if reg.status == "already":
            return f"{project_name} is already registered at {target} ({rw}) — nothing changed."
        if reg.status == "error":
            return f"Error: {target} was not registered — {reg.error}."
        verb = "Promoted the existing filesystem.projects entry for" if reg.status == "promoted" else "Registered"
        source = f"GitHub {github}" if github else "no GitHub origin remote, so the GitHub plugin's picker won't list it"
        return (
            f"{verb} {project_name} ({rw}) at {target} in {_where(reg)} — {source}, default branch "
            f"{default_branch}. Your filesystem tools and the project board read that registry; no further "
            "registration is needed."
        )

    return [onboard_project, register_local_project]
