"""Fenced multi-project filesystem toolset (ADR 0007 — operator primitives).

Generic, opt-in, OFF by default. Gives the agent read / write / list / search +
fenced command execution over a **registry of project directories** — every
path is joined to a managed project root and re-resolved, so nothing can escape
the fence. This is the raw capability a forked operator agent (e.g. "Roxy")
composes into a multi-project manager; the template ships only the inert
primitive — no operator persona, no domain coupling.

Security (ADR 0007 §4):
- Every path resolves under a registry project's root; ``..``/symlink escapes are
  refused (``Path.resolve`` then containment check).
- ``write_file`` / ``edit_file`` require the project's ``write: true`` (a monitor
  fork runs every project read-only).
- ``delete_file`` adds the third Cowork mount mode (ADR 0083 D5): refused when
  ``write: false`` OR ``no_delete: true``, and otherwise always HITL-approved — a
  permanent-delete floor the per-turn ``/bypass`` toggle can't skip.
- ``run_command`` is the dual-use power tool (like ``execute_code``): fenced
  ``cwd``, but arbitrary argv — gated behind ``filesystem.allow_run``.
- Returns clean error strings (never raises into the runner); ``AuditMiddleware``
  records every call; given to subagents only when their allowlist names it.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import regex as _regex  # aliased — search_files's own `regex: bool` PARAM shadows the bare name
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool
from langgraph.prebuilt import InjectedState

from tools.shell import run_command as _shell_run

log = logging.getLogger("protoagent.fs")

_MAX_READ_CHARS = 50_000
_DEFAULT_READ_LINES = 1000  # ~_MAX_READ_CHARS worth of typical 40-60 char source lines
_MAX_LIST = 400
_MAX_MATCHES = 200
_MAX_CONTEXT_LINES = 20  # clamp on search_files(context_lines=…) — a runaway value shouldn't blow the result budget
# search_files(regex=true)'s `query` is model-supplied; stdlib `re` has no match
# timeout, and a catastrophic-backtracking pattern can hang for a caller-controlled
# amount of time regardless of how short the input is (verified: ~30 adversarial
# chars already took 70s, unbounded beyond that — capping input LENGTH doesn't help,
# the blowup happens well within any reasonable cap). `regex` (already a transitive
# dep via tiktoken, promoted to direct) is API-compatible and honors `timeout=`.
_REGEX_MATCH_TIMEOUT_S = 2.0
# A hard cap on TOTAL rendered lines (matches + context), independent of _MAX_MATCHES —
# 200 matches at context_lines=20 would otherwise emit up to 200*41 lines (~40x the old
# tool's ~200-line worst case), since _MAX_MATCHES bounds match COUNT, not output size.
_MAX_SEARCH_OUTPUT_LINES = 400

# What `search_files` walks past by default (#2541). Two different harms, one list:
# a compiled artifact dumped into the transcript is several KB of marshalled bytecode
# the model has to read around, and — worse, because it is silent — a `.pyc` can
# OUTLIVE the source it was built from, so a match there can cite code that no longer
# exists. Vendored/generated trees also duplicate every source hit.
#
# Deliberately the generated-tree set rather than a .gitignore reader: an ignore file
# hides deliberately-untracked *source* (local configs, scratch dirs) that an agent
# often does want to find. Kept tight for the same reason — `build/` and `target/` are
# out because they are real source directories in some repos.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        ".next",
        "coverage",
    }
)
# `grep -I` sniffs a fixed prefix rather than the whole file; a NUL byte in it means
# "not text". Same heuristic, same failure mode (a NUL past the prefix reads as text).
_BINARY_SNIFF_BYTES = 8192


def _read_text_verbatim(path: Path) -> str:
    """Read a file's text WITHOUT universal-newline translation (#2586).

    Python's text mode rewrites ``\\r\\n`` → ``\\n`` on read and ``\\n`` → ``os.linesep`` on
    write. On Windows those two cancelled out for a CRLF file and silently corrupted an LF
    one: ``write_file`` turned every requested ``\\n`` into ``\\r\\n`` on disk, and
    ``read_file`` normalized it straight back — so an agent asked to write LF content and
    verify it read its own request back verbatim and reported PASS while the bytes on disk
    differed. Reading verbatim is what makes that verification mean something.
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _write_text_verbatim(path: Path, text: str) -> None:
    """Write text exactly as given — no newline translation (see :func:`_read_text_verbatim`).

    ``newline=""`` is what keeps a requested ``\\n`` a ``0A`` byte on Windows instead of
    ``0D0A``. Content that genuinely wants CRLF still gets it: the string's own line endings
    are written through untouched.
    """
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _to_crlf(text: str) -> str:
    """``text`` with every line ending as CRLF, for matching inside a CRLF file.

    Now that reads are verbatim, a CRLF file's text contains ``\\r\\n`` while a model's ``old``
    almost always uses bare ``\\n``. Without this the edit would simply not match; and
    normalizing the whole FILE to make it match would rewrite every line ending in it as a side
    effect of a one-line edit. So the needle moves to the file's convention instead.
    """
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _is_probably_binary(path: Path) -> bool:
    """`grep -I` semantics — a NUL byte in the first chunk means 'not text'."""
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True  # unreadable is not searchable either


def _walk_searchable(base: Path) -> tuple[list[Path], bool]:
    """Files under `base` worth grepping, plus whether anything was skipped.

    Prunes with ``os.walk``'s in-place ``dirnames`` rather than filtering ``rglob``
    output, so a `node_modules` is never DESCENDED into — the cost of the old version
    was paid walking the tree, not just printing it. Sorted for a stable result order,
    which ``rglob`` never promised.
    """
    files: list[Path] = []
    skipped = False
    for dirpath, dirnames, filenames in os.walk(base):
        keep = [d for d in dirnames if d not in _SKIP_DIRS]
        skipped = skipped or len(keep) != len(dirnames)
        dirnames[:] = sorted(keep)
        files.extend(Path(dirpath) / name for name in sorted(filenames))
    return files, skipped


_SHELLS = ("default", "cmd", "powershell", "sh")


def _encoded_powershell(command: str) -> str:
    """Base64(UTF-16LE) payload for PowerShell's ``-EncodedCommand``.

    The encoded form carries the command byte-exact through the process
    boundary — no cmd-style re-quoting, and non-ASCII (café / 日本語) survives
    (#2518). The preamble forces UTF-8 output so stdout decodes cleanly off
    the pipe instead of arriving in the OEM code page.
    """
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;" + command
    )
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _platform_shell_argv(command: str, shell: str = "default", *, windows: bool | None = None) -> tuple[list[str], str]:
    """Resolve ``(argv, runner)`` for ``command`` under an explicit shell grammar.

    ``runner`` is the human-readable executable+wrapper chain. It goes into the
    approval dialog so the operator sees what will actually execute (#2518) —
    showing only the inner command hid that Windows always meant ``cmd.exe``,
    letting an approved PowerShell command fail in a shell the operator never saw.
    Raises ``ValueError`` for an unknown or platform-impossible selection.
    """
    if windows is None:
        windows = os.name == "nt"
    if shell not in _SHELLS:
        raise ValueError(f"unknown shell {shell!r} — use one of: {', '.join(_SHELLS)}")
    if shell == "default":
        shell = "cmd" if windows else "sh"
    if shell == "cmd":
        if not windows:
            raise ValueError("shell='cmd' is Windows-only — use 'sh' or 'powershell'.")
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", command], f"{comspec} /d /s /c"
    if shell == "sh":
        if windows:
            raise ValueError("shell='sh' is not available on Windows — use 'cmd' or 'powershell'.")
        return ["/bin/sh", "-c", command], "/bin/sh -c"
    # powershell: Windows PowerShell on Windows, pwsh (PowerShell 7+) elsewhere.
    exe = "powershell.exe" if windows else "pwsh"
    return (
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded_powershell(command)],
        f"{exe} -NoProfile -NonInteractive -EncodedCommand <the command above, UTF-16LE Base64>",
    )


@dataclass
class Project:
    name: str
    root: Path
    write: bool = False
    # Three fence modes match the Cowork local-VM mount modes (ADR 0083 D5, #2012):
    #   write:false                 → read-only
    #   write:true,  no_delete:false → read-write
    #   write:true,  no_delete:true  → read-write-no-delete (create/edit, never delete)
    # ``no_delete`` only bites on ``delete_file``; a read-only project already refuses it.
    no_delete: bool = False


class ProjectRegistry:
    """Resolve ``(project, relative_path)`` to an absolute path fenced under the
    project's root. The single chokepoint every fs tool goes through."""

    def __init__(self, projects: list[Project]):
        self._by_name = {p.name: p for p in projects}

    def names(self) -> list[str]:
        return list(self._by_name)

    def get(self, name: str) -> Project | None:
        return self._by_name.get(name)

    def resolve(self, project: str, rel_path: str = ".") -> Path:
        """Resolve a workspace-relative path. Raises ValueError on unknown
        project or a path that escapes the fence. Does NOT require existence
        (writes create new files)."""
        proj = self._by_name.get(project)
        if proj is None:
            raise ValueError(f"unknown project {project!r}. Known: {', '.join(self._by_name) or '(none)'}")
        rel = (rel_path or ".").strip()
        if rel.startswith("/") or rel.startswith("~"):
            raise ValueError("path must be relative to the project root")
        target = (proj.root / rel).resolve()
        if target != proj.root and proj.root not in target.parents:
            raise ValueError(f"path escapes project {project!r}: {rel_path!r}")
        return target


def _approved(decision) -> bool:
    """Whether the operator approved a gated command. Accepts the resume shapes
    the console may send: a bool, a string ("approve"/"approved"/"yes"/"ok"/…),
    or a dict ({approved: true} / {decision: "approve"})."""
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        if isinstance(decision.get("approved"), bool):
            return decision["approved"]
        decision = decision.get("decision") or decision.get("answer") or ""
    return str(decision).strip().lower() in {"approve", "approved", "yes", "y", "true", "ok"}


def _configured_entries(config, *, create: bool = False) -> list[dict]:
    """The fence entries this config actually asks for — explicit
    ``filesystem.projects``, else the ADR 0095 ``projects:`` registry projected
    onto the fence, else the default workspace. The warning path below reports
    against this same list, so a bad *explicit* entry is named here.

    Registry entries are NOT all visible here: ``fenced_projects`` drops the
    malformed ones (no name/path, duplicate name, relative path) before this
    ever sees them — which is why it does its own WARNING logging rather than
    deferring to the warning below."""
    entries = (
        config.effective_filesystem_projects(create=create)
        if hasattr(config, "effective_filesystem_projects")
        else (getattr(config, "filesystem_projects", []) or [])
    )
    return [e for e in entries or [] if isinstance(e, dict)]


def _registry_from_config(config) -> ProjectRegistry:
    projects: list[Project] = []
    # Explicit projects, or the default workspace dir (created) when none are
    # configured — the on-by-default fenced workspace.
    entries = _configured_entries(config, create=True)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        raw_path = str(entry.get("path") or "").strip()
        if not name or not raw_path:
            log.warning("[fs] skipping project missing name/path: %r", entry)
            continue
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            log.warning("[fs] project %r path is not a directory: %s — skipped", name, root)
            continue
        projects.append(
            Project(
                name=name,
                root=root,
                write=bool(entry.get("write", False)),
                no_delete=bool(entry.get("no_delete", False)),
            )
        )
    return ProjectRegistry(projects)


class _RegistryRef:
    """Live project-registry handle — every fs tool resolves through this (#2836).

    ``build_fs_tools`` runs once per graph build and the tools it returns are
    long-lived closures, so a ``ProjectRegistry`` captured there is a SNAPSHOT:
    when ``onboard_project`` registered a project mid-turn (``HOST.apply_settings``
    reloads the config), the in-flight turn kept executing the old closures and
    the new project stayed invisible until the next turn. ``get()`` re-resolves
    the registry from the live config (the same ``HOST.config`` seam plugin
    routes read) on every call, so a mid-turn registration is visible to the
    very next tool call.

    The build-time config is the fallback, not just a default: headless/test
    contexts never wire the seam, and a RAISING getter (a mid-reload race) must
    degrade to the snapshot behavior — a broken seam never breaks a tool call.
    """

    def __init__(self, build_config):
        self._build_config = build_config
        # The build-time snapshot: the bind/no-bind gate in ``build_fs_tools``
        # is a build-time question, and it doubles as the fallback registry.
        self.build_registry = _registry_from_config(build_config)
        # Identity cache — the server swaps in a whole NEW config object on
        # reload, so ``is`` distinguishes generations without re-statting every
        # project dir (and re-logging skip warnings) on each tool call. Holding
        # the config object keeps its id() from being recycled.
        self._cached_config = build_config
        self._cached_registry = self.build_registry

    def _live_config(self):
        try:
            from graph.plugins.host import HOST

            if HOST.config is not None:
                live = HOST.config()
                if live is not None:
                    return live
        except Exception:  # noqa: BLE001 — degrade to the snapshot, never raise into a tool
            log.warning("[fs] live config read failed — falling back to the build-time config", exc_info=True)
        return self._build_config

    def get(self) -> ProjectRegistry:
        cfg = self._live_config()
        if cfg is not self._cached_config:
            self._cached_registry = _registry_from_config(cfg)
            self._cached_config = cfg
        return self._cached_registry


def _bypass_requested() -> bool:
    """True when the in-flight turn carries the per-turn ``bypass_permissions`` flag — the
    operator's explicit /bypass toggle, sent in the A2A request metadata (read live, so it's
    per-turn, not captured at tool-build time). Gated additionally by ``filesystem_bypass_allowed``
    at the call site, so a host can forbid bypass regardless of caller-supplied metadata."""
    from graph.middleware.request_context import current_request_metadata

    return bool(current_request_metadata().get("bypass_permissions"))


# `read_file` memoization (opt-in, `tools.memoize_reads.enabled`) — found via a
# live-session transcript audit: the same file, read twice non-consecutively in one
# turn, returned byte-identical truncated content both times (stall_guard's stuck-
# loop detector only watches the trailing *contiguous* run, so a non-consecutive
# repeat like this passes it unnoticed). A file's content is static within a turn
# UNLESS the agent itself writes it, so a cached read is invalidated the moment a
# write to the SAME (project, path) lands later in the turn.
#
# Lives inside `read_file` itself (via InjectedState), not as middleware around it:
# an earlier middleware-based design that returned a cached result WITHOUT calling
# the real tool handler silently skipped the `on_tool_end` callback event the rest
# of the stack depends on (the console tool card, tool_durations, the turn's own
# stall-message logic) — verified empirically against a real graph run. Checking
# inside the tool body instead means the tool still "runs" (cheaply — no real read),
# so the normal callback path fires exactly as it does for every other call.
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})


def _this_turn_messages(state) -> list:
    messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", None) or []
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return list(messages[i + 1 :])
    return list(messages)


def _norm_pagination(value, default: int) -> int:
    """Coerce a possibly-absent/None/invalid `offset`/`limit` tool-call arg to the
    same clamped value `read_file` itself would actually use (``max(1, v)``) — NOT
    a naive ``v or default`` fallback, which wrongly jumps an explicit ``0`` (or
    any falsy value) all the way to the unrelated default instead of clamping it to
    1 like the real call does. Getting this wrong desyncs the memoization key from
    what actually ran: an explicit ``limit=0`` call effectively reads 1 line, but
    ``0 or _DEFAULT_READ_LINES`` would compare it as if it read 1000."""
    if value is None:
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _memoized_read(state, project: str, path: str, offset: int, limit: int, limit_was_explicit: bool) -> str | None:
    """The prior read's content to reuse, or None to read for real. Only a call
    whose EXACT (project, path, offset, limit) hasn't been written to since its read
    qualifies — a different offset OR limit is a different chunk of the file, not a
    repeat, so both are keyed in. The LAST event touching that key (read or write),
    scanned in turn order, decides.

    ``limit_was_explicit`` has to be part of the key too, separate from the numeric
    ``limit``: an UNSET limit and an explicit ``limit=1000`` both normalize to the
    same effective value (``_DEFAULT_READ_LINES``), but they can return DIFFERENT
    content — unset auto-extends past 1000 lines when the whole remainder still
    fits the char cap, an explicit 1000 never does. Comparing the numeric value
    alone would let an unset-limit call get served a truncated explicit-limit
    result (or vice versa) as "unchanged," silently hiding content one of the two
    calls actually would have returned."""
    if state is None:
        return None
    messages = _this_turn_messages(state)
    results_by_id = {
        m.tool_call_id: m for m in messages if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    last_content: str | None = None
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in msg.tool_calls or []:
            tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            tc_args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)) or {}
            if tc_args.get("project") != project or tc_args.get("path") != path:
                continue
            if tc_name in _WRITE_TOOLS:
                last_content = None  # a later write invalidates any earlier cached read at ANY offset/limit
            elif (
                tc_name == "read_file"
                and _norm_pagination(tc_args.get("offset"), 1) == offset
                and _norm_pagination(tc_args.get("limit"), _DEFAULT_READ_LINES) == limit
                and (tc_args.get("limit") is not None) == limit_was_explicit
            ):
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                result = results_by_id.get(tc_id)
                if result is not None and getattr(result, "status", "success") != "error":
                    last_content = result.content if isinstance(result.content, str) else None
    return last_content


def build_fs_tools(config) -> list:
    """Build the fenced filesystem tools from config. Empty list when no valid
    projects are registered (so the primitive is inert by default)."""
    # The tools are long-lived closures; they resolve the registry through the
    # ref on EVERY call so a mid-turn registration (#2836) is visible same-turn.
    registry_ref = _RegistryRef(config)
    if not registry_ref.build_registry.names():
        # Configured-but-all-unusable is an OPERATOR MISTAKE, not the inert default: every
        # fs tool unbinds and the agent just... can't read files anymore. Warn, and
        # name the folders, so the log says why instead of only that it happened.
        configured = _configured_entries(config)
        if configured:
            log.warning(
                "[fs] no usable work folders — filesystem tools NOT bound. Fix or remove: %s",
                ", ".join(str(e.get("path") or e.get("name") or "?") for e in configured),
            )
        else:
            log.info("[fs] filesystem enabled but no valid projects registered — no tools")
        return []
    allow_run = bool(getattr(config, "filesystem_allow_run", False))
    memoize_reads = bool(getattr(config, "tools_memoize_reads_enabled", False))
    # run_command is unsandboxed (arbitrary argv as the server user), so by
    # default each invocation is gated behind a HITL approval (the operator sees
    # the command + approves/denies). Forks can disable the gate (e.g. inside a
    # hardened container, or for a trusted autonomous deploy).
    run_requires_approval = bool(getattr(config, "filesystem_run_requires_approval", True))
    # Whether this HOST permits bypass-permissions mode at all (default True). When False, the
    # approval gate is enforced regardless of any caller-supplied bypass metadata.
    bypass_allowed = bool(getattr(config, "filesystem_bypass_allowed", True))

    def _mode(p: Project) -> str:
        if not p.write:
            return "ro"
        return "rw/no-delete" if p.no_delete else "rw"

    @tool
    def list_projects() -> str:
        """List the project workspaces you manage (name, path, and access mode:
        ``ro`` read-only, ``rw`` read-write, ``rw/no-delete`` read-write but deletes off)."""
        registry = registry_ref.get()
        lines = ["Managed projects:"]
        for name in registry.names():
            p = registry.get(name)
            lines.append(f"- {name}  [{_mode(p)}]  {p.root}")
        return "\n".join(lines)

    @tool
    def list_dir(project: str, path: str = ".") -> str:
        """List a directory inside a managed project (path is relative to the project root)."""
        registry = registry_ref.get()
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.is_dir():
            return f"Error: not a directory: {path}"
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        out = [f"{e.name}/" if e.is_dir() else e.name for e in entries[:_MAX_LIST]]
        more = f"\n… (+{len(entries) - _MAX_LIST} more)" if len(entries) > _MAX_LIST else ""
        return "\n".join(out) + more if out else "(empty)"

    @tool
    def read_file(
        project: str,
        path: str,
        offset: int = 1,
        limit: int | None = None,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Read a text file inside a managed project (relative path).

        `offset`/`limit` are LINE numbers (offset=1 is the first line), the same
        addressing `search_files` uses — a hit at "file.py:342" reads straight in
        as `read_file(path="file.py", offset=320, limit=60)` to see it in context,
        no guessing needed. Returns up to `limit` lines starting at `offset`. A
        truncated result names the next offset to pass, so a file larger than one
        chunk is reachable in full across a few calls.

        Leave `limit` unset for the common case: everything from `offset` to EOF
        comes back in one call whenever it fits the ~50K-char safety cap, matching
        a small file's old one-call guarantee regardless of line count (a file
        with 2000 short lines still reads in full if the whole thing is only a
        few KB). Pass an EXPLICIT `limit` to cap the page to exactly that many
        lines even when the file would otherwise fit in one call — e.g. paging
        through a file with many short lines N-at-a-time on purpose. A single
        line longer than ~50K chars (e.g. a minified bundle) is itself cut short
        either way — prefer `search_files` over paging through a line like that.
        """
        registry = registry_ref.get()
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.is_file():
            return f"Error: no such file: {path}"
        offset = max(1, offset)
        explicit_limit = None if limit is None else max(1, limit)
        effective_limit = explicit_limit if explicit_limit is not None else _DEFAULT_READ_LINES
        if (
            memoize_reads
            and _memoized_read(state, project, path, offset, effective_limit, explicit_limit is not None) is not None
        ):
            # A short pointer, not the content again — the saving is real context
            # tokens (the earlier full result is still visible earlier in this
            # turn's history), not just a skipped disk read.
            return f"(unchanged since the identical read earlier this turn — see that read_file result above for {project}/{path}.)"
        try:
            text = _read_text_verbatim(target)
        except OSError as exc:
            return f"Error: cannot read {path}: {exc}"
        lines = text.splitlines(keepends=True)
        total = len(lines)
        # offset=1 must stay valid for an empty file (total=0) — `max(total, 1)`
        # keeps that floor without letting a LARGER offset silently skip the
        # check just because the file happens to be empty.
        if offset > max(total, 1):
            return f"Error: offset {offset} is past the end of {path} ({total} lines)."
        start = offset - 1
        remainder = lines[start:]
        if explicit_limit is None and sum(len(ln) for ln in remainder) <= _MAX_READ_CHARS:
            # No line-count cap was requested, and everything from `offset` to EOF
            # fits the char budget anyway — return it whole. Without this, a file
            # with more than _DEFAULT_READ_LINES short lines but well under the
            # char cap would truncate at the line count alone, a real regression
            # from the old char-only contract (confirmed live: a 2000-line, ~4KB
            # file used to read in one call and would otherwise now cut off at 1000).
            wanted = remainder
        else:
            wanted = lines[start : start + effective_limit]

        # Char safety net independent of `limit` — a run of pathologically long
        # lines (or one minified line) must not blow the budget just because it's
        # "only a few lines." Always include at least one line so a huge single
        # line still makes progress instead of returning nothing.
        selected: list[str] = []
        chars = 0
        line_truncated = False
        for ln in wanted:
            if selected and chars + len(ln) > _MAX_READ_CHARS:
                break
            if not selected and len(ln) > _MAX_READ_CHARS:
                selected.append(ln[:_MAX_READ_CHARS])
                chars += _MAX_READ_CHARS
                line_truncated = True
                break
            selected.append(ln)
            chars += len(ln)
        returned = len(selected)
        chunk = "".join(selected)
        reached_eof = (start + returned) >= total
        if offset == 1 and reached_eof and not line_truncated:
            return chunk  # the common case, unchanged: the whole (small) file in one call
        end_line = offset + returned - 1
        note = f"\n… (showing lines {offset}-{end_line} of {total}"
        if line_truncated:
            note += f"; line {offset} is longer than {_MAX_READ_CHARS} chars and was cut short — try search_files instead of paging through a line like that"
            if not reached_eof:
                # The line itself is unreadable in full, but the FILE isn't done —
                # without this the caller has no way to discover the rest exists.
                note += f"; call again with offset={end_line + 1} for the rest of the file"
        elif not reached_eof:
            note += f"; call again with offset={end_line + 1} for more"
        note += ")"
        return chunk + note

    @tool
    def find_files(project: str, pattern: str = "**/*") -> str:
        """Glob for files in a managed project (e.g. '**/*.py', '.tasks/*.jsonl')."""
        registry = registry_ref.get()
        try:
            root = registry.resolve(project, ".")
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            matches = [p for p in root.glob(pattern) if p.is_file()]
        except (ValueError, OSError) as exc:
            return f"Error: bad pattern: {exc}"
        # .as_posix() (not str()) so the model gets a stable forward-slash contract on
        # Windows too — read_file/edit_file accept forward slashes there. On POSIX it's
        # identical to str(). (#2412)
        rels = [p.relative_to(root).as_posix() for p in matches[:_MAX_MATCHES]]
        more = f"\n… (+{len(matches) - _MAX_MATCHES} more)" if len(matches) > _MAX_MATCHES else ""
        return "\n".join(rels) + more if rels else "(no matches)"

    @tool
    def search_files(
        project: str,
        query: str,
        path: str = ".",
        regex: bool = False,
        context_lines: int = 0,
        include_generated: bool = False,
    ) -> str:
        """Search text files under a managed project path; returns file:line matches —
        grep-style, addressed the same way `read_file`'s offset/limit are, so a hit at
        "file.py:342" reads straight into `read_file(path="file.py", offset=320,
        limit=60)`.

        `query` is a literal substring by default; set regex=true to match it as a
        Python regex instead (e.g. `def \\w+_handler\\(`) — a pattern that times out
        (catastrophic backtracking) aborts the search with an error naming it, so
        simplify it and retry rather than assuming the tool hung. `context_lines`
        (like grep -C) includes that many lines before/after each match, marked `-`
        for context vs `:` for the matching line itself, with adjacent/overlapping
        matches merged into one block and `--` separating non-adjacent ones — so a
        hit comes with enough of the function around it to be useful without a
        separate read_file round trip for the common case.

        Binary files and generated/vendored directories are skipped by default —
        __pycache__, .pytest_cache, .mypy_cache, .ruff_cache, .tox, .git, .venv, venv,
        node_modules, dist, .next, coverage. Set include_generated=true to search them
        too (e.g. to grep a vendored dependency).
        """
        registry = registry_ref.get()
        try:
            base = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        root = registry.resolve(project, ".")

        if regex:
            try:
                pattern = _regex.compile(query)
            except _regex.error as exc:
                return f"Error: bad regex: {exc}"

            def matches(line: str) -> bool:
                # `timeout=` is the actual guarantee against a catastrophic-
                # backtracking pattern (e.g. `(a|a)+b`) — raises the builtin
                # TimeoutError, left uncaught here so it propagates straight out
                # of the search loop below and aborts the whole call immediately,
                # rather than re-triggering the same timeout on every remaining
                # line. Literal search (the default) has no backtracking risk.
                return pattern.search(line, timeout=_REGEX_MATCH_TIMEOUT_S) is not None
        else:

            def matches(line: str) -> bool:
                return query in line

        context_lines = max(0, min(context_lines, _MAX_CONTEXT_LINES))

        skipped_dirs = False
        if base.is_file():
            files = [base]
        elif include_generated:
            files = sorted(p for p in base.rglob("*") if p.is_file())
        else:
            files, skipped_dirs = _walk_searchable(base)

        blocks: list[str] = []
        n_matches = 0
        rendered_lines = 0
        skipped_binary = False
        hit_cap = False
        try:
            for f in files:
                if hit_cap:
                    break
                if not include_generated and _is_probably_binary(f):
                    skipped_binary = True
                    continue
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                # Stop SCANNING once the match cap is reached — a huge file with more
                # than _MAX_MATCHES hits shouldn't pay to enumerate all of them just to
                # throw the rest away below.
                hit_idx: list[int] = []
                for i, line in enumerate(lines):
                    if matches(line):
                        hit_idx.append(i)
                        if len(hit_idx) >= _MAX_MATCHES:
                            break
                if not hit_idx:
                    continue
                rel = f.relative_to(root).as_posix()
                if context_lines == 0:
                    for i in hit_idx:
                        blocks.append(f"{rel}:{i + 1}: {lines[i].strip()[:200]}")
                        n_matches += 1
                        rendered_lines += 1
                        if n_matches >= _MAX_MATCHES or rendered_lines >= _MAX_SEARCH_OUTPUT_LINES:
                            hit_cap = True
                            break
                    continue
                # Merge each match's [-context, +context] window into contiguous ranges
                # so overlapping/adjacent hits render as one block, not duplicated lines.
                ranges: list[list[int]] = []
                for i in hit_idx:
                    lo, hi = max(0, i - context_lines), min(len(lines) - 1, i + context_lines)
                    if ranges and lo <= ranges[-1][1] + 1:
                        ranges[-1][1] = max(ranges[-1][1], hi)
                    else:
                        ranges.append([lo, hi])
                hit_set = set(hit_idx)
                for lo, hi in ranges:
                    if blocks:
                        blocks.append("--")
                        rendered_lines += 1
                    for i in range(lo, hi + 1):
                        is_hit = i in hit_set
                        marker = ":" if is_hit else "-"
                        # The match line strips like the zero-context path does (a hit
                        # renders the same regardless of context_lines); context-only
                        # lines keep their indentation — that's the point of showing them.
                        text = lines[i].strip() if is_hit else lines[i]
                        blocks.append(f"{rel}{marker}{i + 1}{marker} {text[:200]}")
                        rendered_lines += 1
                        if is_hit:
                            n_matches += 1
                        # Checked on EVERY line, not just at range boundaries — a single
                        # merged range (e.g. from context_lines overlapping many nearby
                        # hits) must not blow past the budget before the next check.
                        if n_matches >= _MAX_MATCHES or rendered_lines >= _MAX_SEARCH_OUTPUT_LINES:
                            hit_cap = True
                            break
                    if hit_cap:
                        break
        except TimeoutError:
            return f"Error: regex took too long to match (possible catastrophic backtracking) — simplify the pattern: {query!r}"
        if hit_cap:
            # Two independent caps can trigger this: too many MATCHES, or too much
            # OUTPUT (e.g. wide context_lines around matches that were all already
            # found) — don't claim specifically "more matches" when it might just be
            # more context around matches already shown in full.
            return "\n".join(blocks) + "\n… (more matches or output; narrow the search or lower context_lines)"
        if blocks:
            return "\n".join(blocks)
        # Say what was NOT searched, so "(no matches)" can't be read as "not in this
        # repo" when the answer is sitting in a pruned tree.
        if skipped_dirs or skipped_binary:
            return "(no matches; binary files and generated dirs were skipped — retry with include_generated=true to search those too)"
        return "(no matches)"

    @tool
    def write_file(project: str, path: str, content: str) -> str:
        """Write (create/overwrite) a text file in a read-write managed project."""
        registry = registry_ref.get()
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        proj = registry.get(project)
        if not proj.write:
            return f"Error: project {project!r} is read-only (write:false)."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            _write_text_verbatim(target, content)
        except OSError as exc:
            return f"Error: cannot write {path}: {exc}"
        return f"{'Overwrote' if existed else 'Created'} {path} ({len(content)} chars)."

    @tool
    def edit_file(project: str, path: str, old: str, new: str) -> str:
        """Replace the first exact occurrence of `old` with `new` in a file (read-write project)."""
        registry = registry_ref.get()
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        proj = registry.get(project)
        if not proj.write:
            return f"Error: project {project!r} is read-only (write:false)."
        if not target.is_file():
            return f"Error: no such file: {path}"
        text = _read_text_verbatim(target)
        needle, replacement = old, new
        if needle not in text and "\r\n" in text:
            # A CRLF file and an LF needle (what a model almost always sends). Match in the
            # file's own convention rather than normalizing the file, so a one-line edit
            # doesn't rewrite every line ending in it.
            needle, replacement = _to_crlf(old), _to_crlf(new)
        if needle not in text:
            return f"Error: `old` not found in {path}."
        if text.count(needle) > 1:
            return f"Error: `old` is not unique in {path} ({text.count(needle)} matches) — add context."
        try:
            _write_text_verbatim(target, text.replace(needle, replacement, 1))
        except OSError as exc:
            return f"Error: cannot write {path}: {exc}"
        return f"Edited {path}."

    @tool
    def delete_file(project: str, path: str) -> str:
        """Permanently delete a single file inside a managed project (relative path).

        Refused in read-only projects and in ``no_delete`` (read-write-no-delete) projects.
        Otherwise it ALWAYS pauses for the operator to approve first — a permanent-delete
        floor that even bypass-permissions mode can't skip, because deletion is irreversible.
        Removes one file, not a directory (use ``run_command`` for directory trees).
        """
        registry = registry_ref.get()
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        proj = registry.get(project)
        if not proj.write:
            return f"Error: project {project!r} is read-only (write:false)."
        if proj.no_delete:
            return f"Error: project {project!r} forbids deletes (no_delete:true)."
        if target.is_dir():
            return f"Error: {path} is a directory — delete_file removes a single file (use run_command for trees)."
        if not target.is_file():
            return f"Error: no such file: {path}"
        # Permanent-delete floor (ADR 0083 D5, #2012): ALWAYS ask, regardless of the per-turn
        # /bypass toggle that skips run_command's gate — an irreversible op gets no auto-skip.
        from langgraph.types import interrupt

        decision = interrupt(
            {"kind": "approval", "title": "Approve permanent file delete?", "detail": path, "project": project}
        )
        if not _approved(decision):
            # RETURN (not raise) a decline, same as run_command: a decline is the operator's
            # choice, not a tool error, and raising rendered an undismissable red block.
            return (
                f"Delete declined by the operator — not deleted: {path!r}. "
                "Do not retry; wait for the operator's next instruction or ask how to proceed."
            )
        try:
            target.unlink()
        except OSError as exc:
            return f"Error: cannot delete {path}: {exc}"
        return f"Deleted {path}."

    tools = [list_projects, list_dir, read_file, find_files, search_files, write_file, edit_file, delete_file]

    if allow_run:

        @tool
        async def run_command(project: str, command: str, timeout: float = 60.0, shell: str = "default") -> str:
            """Run a shell command inside a managed project's directory (fenced cwd).

            Powerful + dual-use (like execute_code) — use it for read-only
            inspection (`git status`, `gh pr list`, `br list`) and only mutate in
            read-write projects. ``shell`` picks the grammar explicitly:
            "default" = ``cmd.exe`` on Windows / ``/bin/sh`` elsewhere;
            "powershell" = Windows PowerShell (``pwsh`` off-Windows), Unicode-safe;
            "cmd" / "sh" name the platform defaults. PowerShell syntax
            (``Set-Content``, cmdlets, ``$vars``) REQUIRES shell="powershell" —
            on Windows the default grammar is cmd.exe and will not run it.
            """
            registry = registry_ref.get()
            try:
                root = registry.resolve(project, ".")
            except ValueError as exc:
                return f"Error: {exc}"
            if not command.strip():
                return "Error: empty command."
            try:
                argv, runner = _platform_shell_argv(command, shell)
            except ValueError as exc:
                return f"Error: {exc}"
            # HITL approval gate (Sprint A): pause for the operator to approve
            # the command before it runs. interrupt() re-runs this fn from the
            # top on resume (the validation above is idempotent) and returns the
            # operator's decision. Denied → don't run.
            if run_requires_approval and not (bypass_allowed and _bypass_requested()):
                from langgraph.types import interrupt

                decision = interrupt(
                    {
                        "kind": "approval",
                        "title": "Approve shell command?",
                        # The runner line shows the real executable chain, not just the
                        # inner command — approving PowerShell text that secretly ran
                        # under cmd.exe is exactly the #2518 failure.
                        "detail": f"{command}\n\nruns via: {runner}",
                        "project": project,
                    }
                )
                if not _approved(decision):
                    # RETURN (not raise): a decline is the operator's deliberate choice,
                    # not a tool failure — raising stamped status="error", which the chat
                    # rendered as a full-bleed red block the operator couldn't dismiss
                    # (and read as "something broke"). A normal result renders as an
                    # ordinary collapsed tool card. The message tells the model to stop,
                    # not retry, so a decline ends the attempt cleanly.
                    return (
                        f"Command declined by the operator — not run: {command!r}. "
                        "Do not re-run it; wait for the operator's next instruction or ask how to proceed."
                    )
            elif run_requires_approval:
                # Bypass-permissions mode (the operator's explicit per-turn /bypass toggle): the
                # approval gate is skipped. AUDIT every command that runs without confirmation.
                log.warning(
                    "[fs] run_command ran under bypass-permissions (no approval): %s (via %s)",
                    command,
                    runner,
                )
            res = await _shell_run(argv, cwd=str(root), timeout=timeout)
            if res.error:
                raise ToolException(res.error)
            body = res.stdout or "(no output)"
            if res.stderr:
                body += f"\n[stderr]\n{res.stderr}"
            return body[:_MAX_READ_CHARS] + (f"\n(exit {res.returncode})" if res.returncode else "")

        tools.append(run_command)

    log.info("[fs] %d project(s), %d tool(s), run=%s", len(registry_ref.build_registry.names()), len(tools), allow_run)
    return tools
