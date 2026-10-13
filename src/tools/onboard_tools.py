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
- ``onboarding.root`` — every checkout lands here, and a local registration that
  RESOLVES under it goes straight through. A local folder outside it (or any, while
  it is unset) is registered only on the operator's per-folder approval card
  (``onboarding.approve_outside_root``; off → refused, the pre-card behavior). A
  clone target that would escape the root is refused before anything is written.
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
import sys
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


_FETCH_TIMEOUT_S = 60


def _refresh_checkout(checkout: Path) -> str:
    """Fetch a REUSED ``checkout``'s upstream and fast-forward it — only when that can
    never lose work (#3733). Opt-in (``refresh=true``); the default reuse path still
    never touches the directory.

    Refuses, naming the reason, rather than ever resetting or merging: tracked local
    changes, no upstream, or local commits the upstream doesn't have (a fast-forward
    is impossible and a merge/reset would be a judgement call for a human). Untracked
    files are left alone — ``merge --ff-only`` itself refuses if one would be
    overwritten. Returns one sentence to append to the reuse message."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _git(*argv: str, timeout: int = 10):
        return subprocess.run(
            ["git", *argv],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )

    try:
        up = _git("rev-parse", "--abbrev-ref", "@{upstream}")
        if up.returncode != 0 or not (up.stdout or "").strip():
            return "Not refreshed — the current branch has no upstream tracking branch to fetch."
        upstream = up.stdout.strip()
        dirty = _git("status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0:
            return "Not refreshed — it is not a readable git checkout."
        if (dirty.stdout or "").strip():
            return "Not refreshed — it has uncommitted changes to tracked files (left exactly as they are)."
        remote = upstream.split("/", 1)[0]
        fetched = _git("fetch", "--quiet", "--", remote, timeout=_FETCH_TIMEOUT_S)
        if fetched.returncode != 0:
            err = (fetched.stderr or "").strip() or f"exit code {fetched.returncode}"
            return f"Not refreshed — git fetch {remote} failed: {_redact(err)}"
        counts = _git("rev-list", "--left-right", "--count", f"{upstream}...HEAD")
        fields = (counts.stdout or "").split()
        if counts.returncode != 0 or len(fields) != 2 or not all(f.isdigit() for f in fields):
            return f"Fetched {upstream}, but how far it moved could not be determined; not fast-forwarded."
        behind, ahead = int(fields[0]), int(fields[1])
        if ahead:
            return (
                f"Fetched, but not fast-forwarded — it has {ahead} local commit{'' if ahead == 1 else 's'} "
                f"{upstream} doesn't ({behind} behind); reconciling that is left to you."
            )
        if not behind:
            return f"Fetched — it is up to date with {upstream}."
        ff = _git("merge", "--ff-only", "--quiet", upstream, timeout=_FETCH_TIMEOUT_S)
        if ff.returncode != 0:
            err = (ff.stderr or "").strip() or f"exit code {ff.returncode}"
            return f"Fetched ({behind} behind {upstream}), but the fast-forward was refused: {_redact(err)}"
        head = (_git("rev-parse", "--short", "HEAD").stdout or "").strip()
        return f"Fetched and fast-forwarded {behind} commit{'' if behind == 1 else 's'} to {upstream} ({head})."
    except subprocess.TimeoutExpired:
        return "Not refreshed — git timed out (the remote may be unreachable)."
    except Exception as exc:  # noqa: BLE001 — a tool must not raise into the turn
        return f"Not refreshed — git could not run: {_redact(str(exc))}"


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


# ---------------------------------------------------------------------------
# Outside-root registration: an operator approval card, per folder
# ---------------------------------------------------------------------------
#
# ``register_local_project`` on a directory OUTSIDE ``onboarding.root`` parks for the
# operator instead of refusing (``onboarding.approve_outside_root``, default on). The
# card is composed ENTIRELY by the server from the resolved path — nothing the agent
# wrote is rendered as prose — and its answer is bound to that exact realpath, so the
# folder the operator approved is the folder that gets registered. The root stays the
# boundary for everything else; one approval registers one folder.
#
# NOT skippable by the per-turn /bypass toggle, by the console's "Approve & don't ask
# again", or by the Zed shim's "Allow for this session". Those skip confirmation of
# actions INSIDE the fence the operator already drew (run_command in a registered
# project). This gate MOVES the fence — a config write that outlives the turn and the
# session — so a standing "yes" would hand the agent the power to widen its own
# filesystem reach at will. The permanent-delete floor (ADR 0083 D5) is the precedent
# for a gate bypass cannot skip; this one's reason is persistence, not irreversibility.

_ALLOW_RO = "allow-read-only"
_ALLOW_RW = "allow-read-write"
_DENY = "deny"
# A plain approve — what a client that predates the ``options`` field sends, and ALSO
# what every auto-approver sends (an older Zed shim's "Allow for this session", the
# console's "Approve & don't ask again"). It can't be tied to the folder on the card and
# can't be told apart from a standing "yes", so it registers NOTHING: the result says
# to answer from a client that shows the access choices.
_PLAIN_APPROVE = {"approve", "approved", "yes", "y", "true", "ok"}

# Refused outright — no card. Every entry is compared against the REALPATH.
# Subtrees (the dir itself and everything under it):
_SYSTEM_SUBTREES_POSIX = (
    "/System", "/Library", "/Applications", "/usr", "/etc", "/bin", "/sbin", "/var",
    "/private", "/dev", "/proc", "/sys", "/boot", "/run", "/lib", "/lib32", "/lib64",
    "/libx32", "/opt", "/snap", "/nix", "/root", "/cores", "/Network",
)  # fmt: skip
# ...except these scratch areas, whose strict SUBDIRECTORIES may be approved (a temp
# checkout is a normal thing to want to look at; the temp dir itself never is).
_TEMP_PARENTS_POSIX = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders")
# The dir itself only (a mount/user parent): children may be approved, the parent never.
_EXACT_POSIX = ("/Users", "/home", "/Volumes", "/mnt", "/media", "/srv")
_SYSTEM_SUBTREES_NT = ("Windows", "Program Files", "Program Files (x86)", "ProgramData")

# Directory names that hold credentials — refused anywhere in the path.
_CREDENTIAL_DIRS = frozenset(
    {".ssh", ".gnupg", ".gpg", ".aws", ".azure", ".kube", ".docker", ".password-store",
     ".1password", "keychains", ".gcloud", "gcloud", ".vault", ".terraform.d"}
)  # fmt: skip
# Directly under the home dir: dirs that aggregate every app's config/credentials.
# ``~/Library`` (macOS) and ``~/AppData`` (Windows) are refused as whole subtrees —
# keychains, cookies, every app's state, the desktop app's own box root.
_HOME_SUBTREES = ("Library", "AppData")
_HOME_EXACT = (".config", ".local", ".cache", ".protoagent")

# Control characters, plus the invisible/bidi ones that could make a folder name READ as
# something else on the card (a right-to-left override, a zero-width joiner).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

# macOS and Windows filesystems are case-insensitive by default, and ``resolve()`` keeps
# the caller's casing there — ``/USR/bin`` and ``/users/kj`` resolve to themselves. So the
# floor compares case-folded on those platforms, or a re-cased path would walk around it.
_CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"


def _norm(p: Path | str) -> str:
    s = str(p)
    return s.casefold() if _CASE_INSENSITIVE else s


def _within(child: Path, parent: Path) -> bool:
    """``child`` is ``parent`` or under it — case-insensitively on macOS / Windows."""
    c, p = _norm(child), _norm(parent)
    sep = "\\" if os.name == "nt" else "/"
    return c == p or c.startswith(p.rstrip(sep) + sep)


def _protoagent_state_roots() -> list[Path]:
    """Every protoAgent box/instance root this process knows — the agent's own config,
    secrets and stores. Best-effort: a resolution failure contributes nothing."""
    roots: list[Path] = []
    try:
        from infra.paths import instance_paths, known_box_roots

        roots.extend(known_box_roots())
        roots.append(instance_paths().instance_root.resolve())
    except Exception:  # noqa: BLE001 — the rest of the floor still applies
        log.debug("[onboard] could not resolve protoAgent state roots", exc_info=True)
    return roots


def _hard_refusal(target: Path) -> str | None:
    """Why ``target`` (a REALPATH) can never be registered from outside the root — even
    with the operator's approval — or ``None`` when an approval card may be shown.

    Deliberately conservative: a place where approving is essentially never right, and
    where one tired click would expose the machine, gets no card to click."""
    if _CONTROL_RE.search(str(target)):
        return "the path contains control characters"
    if target == Path(target.anchor):
        return "it is the filesystem root"
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        if _within(home, target):
            return "it is your home directory" if _norm(home) == _norm(target) else "it contains your home directory"
        for sub in _HOME_SUBTREES:
            if _within(target, home / sub):
                return f"it is inside ~/{sub} (application state and credentials)"
        for sub in _HOME_EXACT:
            if _norm(target) == _norm(home / sub):
                return f"~/{sub} holds application config and credentials"
    for state in _protoagent_state_roots():
        if _within(target, state) or _within(state, target):
            return "it is (or contains) protoAgent's own config and data directory"
    parts = [p.casefold() for p in target.parts]
    hit = next((p for p in parts if p in _CREDENTIAL_DIRS), None)
    if hit:
        return f"it is inside a credentials directory ({hit})"
    from tools.fs_secrets import is_secret_path

    secret = is_secret_path(target)
    if secret:
        return f"it looks like {secret}"
    if os.name == "nt":
        anchor = Path(target.anchor)
        for sub in _SYSTEM_SUBTREES_NT:
            if _within(target, anchor / sub):
                return f"it is a system directory ({anchor / sub})"
        return None
    s = _norm(target)
    if s in {_norm(t) for t in _TEMP_PARENTS_POSIX}:
        return f"{target} is a shared scratch area — register one project folder inside it"
    if any(s.startswith(_norm(t) + "/") for t in _TEMP_PARENTS_POSIX):
        return None  # a strict subdirectory of a scratch area
    for sys_dir in _SYSTEM_SUBTREES_POSIX:
        if _within(target, Path(sys_dir)):
            return f"it is a system directory ({sys_dir})"
    if s in {_norm(t) for t in _EXACT_POSIX}:
        return f"{target} holds every user's / volume's directories — register one project inside it"
    return None


def _path_token(target: Path) -> str:
    """A short digest of the realpath the card shows — the approve answers carry it, so
    an answer can only register the folder that was on the card (the tool re-runs from
    the top on resume and re-resolves the path; a folder swapped in between is caught)."""
    import hashlib

    return hashlib.sha256(str(target).encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _clean_display(text: str, limit: int = 200) -> str:
    """One line, no control characters, bounded — for anything on the card that came
    off disk (an origin URL) or from the agent (a project name)."""
    text = _CONTROL_RE.sub("?", str(text or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_origin(target: Path) -> tuple[bool, str]:
    """``(is_git_checkout, origin_url)`` read from ``target/.git`` WITHOUT running git —
    the folder isn't approved yet, and a hostile repo config (``core.fsmonitor`` …) can
    make some git commands execute code. Best-effort: anything odd → ``(False, "")``."""
    dotgit = target / ".git"
    try:
        if dotgit.is_file():  # a worktree / submodule: "gitdir: <path>"
            line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                return False, ""
            gitdir = Path(line[len("gitdir:") :].strip())
            gitdir = gitdir if gitdir.is_absolute() else target / gitdir
            # A worktree's config lives in the COMMON dir.
            common = gitdir / "commondir"
            if common.is_file():
                c = Path(common.read_text(encoding="utf-8", errors="replace").strip())
                gitdir = c if c.is_absolute() else gitdir / c
            config_file = gitdir / "config"
        elif dotgit.is_dir():
            config_file = dotgit / "config"
        else:
            return False, ""
        text = config_file.read_text(encoding="utf-8", errors="replace")[:65536] if config_file.is_file() else ""
    except OSError:
        return False, ""
    origin = ""
    in_origin = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_origin = bool(re.match(r'^\[\s*remote\s+"origin"\s*\]$', line))
            continue
        if in_origin:
            m = re.match(r"^url\s*=\s*(.+)$", line)
            if m:
                origin = m.group(1).strip().strip('"')
                break
    return True, origin


def _outside_root_decision(decision, token: str) -> str:
    """Map the operator's answer to ``"ro"`` / ``"rw"`` / ``"deny"`` / ``"stale"`` /
    ``"plain"``.

    Only the two allow values bound to THIS path mean yes. ``"stale"`` = an allow answer
    bound to a different path than this run resolved; ``"plain"`` = an unbound approve
    (see ``_PLAIN_APPROVE``). Anything else — a dismissal, the autonomous-turn sentinel,
    garbage — is a no."""
    if isinstance(decision, bool):
        return "plain" if decision else "deny"
    if isinstance(decision, dict):
        if isinstance(decision.get("approved"), bool):
            return "plain" if decision["approved"] else "deny"
        decision = decision.get("decision") or decision.get("answer") or ""
    answer = str(decision or "").strip().lower()
    for prefix, mode in ((_ALLOW_RO, "ro"), (_ALLOW_RW, "rw")):
        if answer == f"{prefix}@{token}":
            return mode
        if answer.startswith(prefix + "@"):
            return "stale"
    if answer in _PLAIN_APPROVE:
        return "plain"
    return "deny"


def _outside(root_raw: str) -> str:
    """How a result names where the folder sits relative to the root — which may be unset."""
    if root_raw.strip():
        return f"outside the onboarding root ({root_raw})"
    return "with no onboarding root set"


def _approval_card(
    *, target: Path, raw: str, root_raw: str, project_name: str, requested_write: bool, token: str
) -> dict:
    """The hitl-v1 ``approval`` payload. Every line of ``detail`` is composed here from
    the REALPATH; the agent's words reach it only as the (sanitized, quoted) name and the
    as-typed path when that differs, both labelled as what they are. ``detail`` renders
    as a literal block (never markdown) in the console, the deck and Zed."""
    is_git, origin = _read_origin(target)
    if is_git and origin:
        git_line = f"git checkout, origin {_clean_display(_redact(origin))}"
    elif is_git:
        git_line = "git checkout, no origin remote"
    else:
        git_line = "not a git checkout"
    lines = [f"Folder:     {target}"]
    if str(Path(raw).expanduser()) != str(target):
        lines.append(f"Requested:  {_clean_display(raw)!r} (resolves to the folder above)")
    lines += [
        f"Repository: {git_line}",
        f"Name:       {_clean_display(project_name, 80)!r}",
        f"Agent asks: {'read-write' if requested_write else 'read-only'}",
        (
            f"Outside:    onboarding root {_clean_display(root_raw)}"
            if root_raw.strip()
            else "Outside:    no onboarding root is set — this agent has no default workspace; approving "
            "registers only this folder"
        ),
        "",
        "Allowing registers THIS folder as a managed project (your filesystem tools, the",
        "project board and the GitHub plugin will reach it). Nothing else is widened. Your",
        "choice of access wins over what the agent asked for.",
    ]
    return {
        "kind": "approval",
        "title": "Allow access to a folder outside the onboarding root?",
        "detail": "\n".join(lines),
        "tool": "register_local_project",
        "path": str(target),
        # This gate moves the fence, so no client may offer a standing "yes" for it.
        "session_allow": False,
        "options": [
            {"value": f"{_ALLOW_RO}@{token}", "label": "Allow read-only", "kind": "allow_once", "primary": True},
            {"value": f"{_ALLOW_RW}@{token}", "label": "Allow read-write", "kind": "allow_once"},
            {"value": _DENY, "label": "Deny", "kind": "reject_once"},
        ],
    }


def _existing_entry(config, target: Path) -> dict | None:
    """The registry (else explicit fence) entry already naming ``target``, read from the
    LIVE config like :func:`_register` does — or ``None``."""
    from graph.plugins.host import HOST

    source = config
    if HOST.config is not None:
        try:
            source = HOST.config() or config
        except Exception:  # noqa: BLE001
            source = config
    for attr in ("projects", "filesystem_projects"):
        for e in getattr(source, attr, []) or []:
            if isinstance(e, dict) and _same_path(e.get("path"), target):
                return e
    return None


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
        refresh: bool = False,
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
            refresh: For a checkout that is ALREADY on disk: ``git fetch`` its
                upstream and fast-forward it. Only ever a fast-forward — refused
                (with the reason) when it has uncommitted tracked changes, no
                upstream, or local commits the upstream lacks. Use it before
                reading or auditing a clone that may be stale.

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
          That count is only as fresh as the last fetch — pass ``refresh=true``
          to fetch and fast-forward instead.
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
        if reused_checkout and refresh:
            drift = " " + await asyncio.to_thread(_refresh_checkout, target)
        elif reused_checkout:
            drift = " " + await asyncio.to_thread(_tracking_drift, target)
            if "it was not fetched" in drift:
                drift += " Pass refresh=true to fetch and fast-forward it."

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
            if not reused_checkout:
                # Registered, but the folder was gone — we just cloned it back (#3643). Saying
                # "reused … nothing changed" here hid that the checkout had been missing, and a
                # missing registered root is exactly what unbinds the filesystem tools.
                return (
                    f"{project_name} is already registered at {target} ({rw}), but the checkout was "
                    f"missing — cloned {ref.display_url} back into it (default branch {default_branch}). "
                    "If read_file / search_files dropped out of this session while the folder was gone, "
                    "they come back in a new chat; say so rather than reading files another way."
                )
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

    async def _register_outside_root(*, raw: str, target: Path, root_raw: str, name: str | None, write: bool | None) -> str:
        """``register_local_project`` for a realpath OUTSIDE the root: hard floor, then the
        operator's approval card, then the shared registry write. Re-entered from the top
        on resume (LangGraph re-runs the tool), so every check below runs again on the
        path as it resolves NOW, and the answer must be bound to that same path."""
        refusal = _hard_refusal(target)
        if refusal:
            log.warning("[onboard] outside-root registration hard-refused (%s): %s", refusal, target)
            return (
                f"Refused: {raw} resolves to {target}, {_outside(root_raw)}, and "
                f"{refusal} — this location can't be registered, even with the operator's approval. "
                "Pick a project directory instead."
            )
        if not target.is_dir():
            return f"Error: {target} is not an existing directory."
        if name is not None and (_CONTROL_RE.search(name) or not name.strip()):
            return "Error: name must be a non-empty single line without control characters."
        project_name = (name or target.name).strip()

        # Already registered (by the operator, or an earlier approval) → nothing moves the
        # fence, so no card. A fence-only entry is PROMOTED at its OWN write mode — never
        # the agent's — so this can't be used to upgrade a read-only folder.
        existing = _existing_entry(config, target)
        if existing is not None:
            mode = bool(existing.get("write", False))
            rw = "read-write" if mode else "read-only"
            reg = await _register(
                config,
                target=target,
                project_name=str(existing.get("name") or project_name),
                write=mode,
                github=str(existing.get("github") or ""),
                default_branch=str(existing.get("default_branch") or "main"),
            )
            if reg.status == "error":
                return f"Error: {target} was not registered — {reg.error}."
            if reg.status == "already":
                return f"{existing.get('name') or project_name} is already registered at {target} ({rw}) — nothing changed."
            return f"Promoted the existing filesystem.projects entry for {existing.get('name') or project_name} ({rw}) at {target} in {_where(reg)}."

        requested_write = write if write is not None else bool(getattr(config, "onboarding_write_default", False))
        token = _path_token(target)
        card = _approval_card(
            target=target,
            raw=raw,
            root_raw=root_raw,
            project_name=project_name,
            requested_write=requested_write,
            token=token,
        )
        # ALWAYS asks: no bypass_permissions / "allow for session" check here, on purpose
        # (see the section comment above _ALLOW_RO).
        from langgraph.types import interrupt

        choice = _outside_root_decision(interrupt(card), token)
        if choice == "stale":
            log.warning("[onboard] outside-root approval did not match the resolved path — not registered: %s", target)
            return (
                f"Not registered: the approval answered a different folder than {target} resolves to now "
                "(it changed while the card was open). Nothing was written; ask again if you still need it."
            )
        if choice == "plain":
            log.warning("[onboard] outside-root approval answered with a plain approve — not registered: %s", target)
            return (
                f"Not registered: {target} ({_outside(root_raw)}) needs the operator to pick an "
                "access level (Allow read-only / Allow read-write), but the answer was a plain approve — from a "
                "client that doesn't show those choices, or an automatic approval. Nothing was written. Ask the "
                "operator to answer from the console or Zed, or to add the folder in Settings."
            )
        if choice == "deny":
            log.info("[onboard] outside-root registration DENIED by the operator: %s", target)
            return (
                f"Denied by the operator — {target} was not registered ({_outside(root_raw)}). Do not ask "
                "again for this folder in this turn; offer an alternative instead, e.g. clone the repo with "
                "onboard_project, or ask the operator to set or widen onboarding.root."
            )

        write_effective = choice == "rw"
        rw = "read-write" if write_effective else "read-only"
        log.warning(
            "[onboard] outside-root registration APPROVED by the operator (%s, agent asked %s): %s (root %s)",
            rw,
            "read-write" if requested_write else "read-only",
            target,
            root_raw or "(unset)",
        )
        from graph.workspaces.manager import github_slug_for_checkout

        # git runs here only AFTER the operator approved the folder.
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
        if reg.status == "error":
            return f"Error: approved by the operator, but {target} was not registered — {reg.error}."
        overridden = (
            f" (you asked for {'read-write' if requested_write else 'read-only'}; the operator's choice wins)"
            if requested_write != write_effective
            else ""
        )
        source = f"GitHub {github}" if github else "no GitHub origin remote, so the GitHub plugin's picker won't list it"
        return (
            f"Registered {project_name} ({rw}) at {target} in {_where(reg)} — approved by the operator "
            f"({rw}){overridden}; it is {_outside(root_raw)}, and only this folder was added. "
            f"{source}, default branch {default_branch}."
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

        BOUNDED by ``onboarding.root``: a path that RESOLVES (symlinks followed) to
        an existing directory strictly inside that root registers directly. A
        directory OUTSIDE the root (or any directory, when no root is set) pauses and
        shows the operator an approval card
        (Allow read-only / Allow read-write / Deny) for that one folder — just call
        this tool with the path; the operator's choice of access wins over
        ``write``. If they deny it, the result says so: don't retry, offer an
        alternative (clone it into the root with ``onboard_project``). Some places
        are refused outright with no card — the filesystem root, the home
        directory, system and credential directories. ``onboarding.allow`` does not
        apply (nothing is fetched). Already registered → success with a note;
        registration is idempotent.

        Returns a confirmation naming the project, its path, and whether it is
        read-only or read-write — or a ``Refused:``/``Error:``/``Denied`` string.
        """
        raw = (path or "").strip()
        if not raw:
            return "Error: no path was given — pass the absolute path of the directory to register."
        root_raw = getattr(config, "onboarding_root", "") or ""
        approve_outside = bool(getattr(config, "onboarding_approve_outside_root", True))
        if not root_raw.strip() and not approve_outside:
            return _ROOT_UNSET.format(verb="register from")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            return f"Error: {raw!r} is not an absolute path — pass the full path (a leading ~ is fine)."
        if not root_raw.strip():
            # No root (a stock install): EVERY folder is "outside", so every registration
            # goes to the operator's card — same floor, same binding, same no-bypass rule.
            return await _register_outside_root(
                raw=raw, target=expanded.resolve(), root_raw="", name=name, write=write
            )
        root_resolved = Path(root_raw).expanduser().resolve()
        target = expanded.resolve()
        # Containment BEFORE existence: a path outside the root is refused the same
        # way whether or not it exists, so the tool can't be used to probe the disk.
        if not target.is_relative_to(root_resolved):
            if not approve_outside:
                return (
                    f"Refused: {raw} resolves to {target}, outside the onboarding root ({root_raw}) — only "
                    "directories under the root can be registered. The operator widens this by changing "
                    "onboarding.root."
                )
            return await _register_outside_root(raw=raw, target=target, root_raw=root_raw, name=name, write=write)
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
