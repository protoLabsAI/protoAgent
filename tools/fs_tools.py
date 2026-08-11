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

from langchain_core.tools import ToolException, tool

from tools.shell import run_command as _shell_run

log = logging.getLogger("protoagent.fs")

_MAX_READ_CHARS = 50_000
_MAX_LIST = 400
_MAX_MATCHES = 200

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


def _bypass_requested() -> bool:
    """True when the in-flight turn carries the per-turn ``bypass_permissions`` flag — the
    operator's explicit /bypass toggle, sent in the A2A request metadata (read live, so it's
    per-turn, not captured at tool-build time). Gated additionally by ``filesystem_bypass_allowed``
    at the call site, so a host can forbid bypass regardless of caller-supplied metadata."""
    from graph.middleware.request_context import current_request_metadata

    return bool(current_request_metadata().get("bypass_permissions"))


def build_fs_tools(config) -> list:
    """Build the fenced filesystem tools from config. Empty list when no valid
    projects are registered (so the primitive is inert by default)."""
    registry = _registry_from_config(config)
    if not registry.names():
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
        lines = ["Managed projects:"]
        for name in registry.names():
            p = registry.get(name)
            lines.append(f"- {name}  [{_mode(p)}]  {p.root}")
        return "\n".join(lines)

    @tool
    def list_dir(project: str, path: str = ".") -> str:
        """List a directory inside a managed project (path is relative to the project root)."""
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
    def read_file(project: str, path: str) -> str:
        """Read a text file inside a managed project (relative path). Truncated if large."""
        try:
            target = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not target.is_file():
            return f"Error: no such file: {path}"
        try:
            text = _read_text_verbatim(target)
        except OSError as exc:
            return f"Error: cannot read {path}: {exc}"
        if len(text) > _MAX_READ_CHARS:
            return text[:_MAX_READ_CHARS] + f"\n… (truncated at {_MAX_READ_CHARS} chars)"
        return text

    @tool
    def find_files(project: str, pattern: str = "**/*") -> str:
        """Glob for files in a managed project (e.g. '**/*.py', '.tasks/*.jsonl')."""
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
    def search_files(project: str, query: str, path: str = ".", include_generated: bool = False) -> str:
        """Substring-search text files under a managed project path; returns file:line matches.

        Binary files and generated/vendored directories are skipped by default —
        __pycache__, .pytest_cache, .mypy_cache, .ruff_cache, .tox, .git, .venv, venv,
        node_modules, dist, .next, coverage. Set include_generated=true to search them
        too (e.g. to grep a vendored dependency).
        """
        try:
            base = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        root = registry.resolve(project, ".")

        skipped_dirs = False
        if base.is_file():
            files = [base]
        elif include_generated:
            files = sorted(p for p in base.rglob("*") if p.is_file())
        else:
            files, skipped_dirs = _walk_searchable(base)

        hits: list[str] = []
        skipped_binary = False
        for f in files:
            if not include_generated and _is_probably_binary(f):
                skipped_binary = True
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        hits.append(f"{f.relative_to(root).as_posix()}:{i}: {line.strip()[:200]}")
                        if len(hits) >= _MAX_MATCHES:
                            return "\n".join(hits) + "\n… (more matches; narrow the search)"
            except OSError:
                continue
        if hits:
            return "\n".join(hits)
        # Say what was NOT searched, so "(no matches)" can't be read as "not in this
        # repo" when the answer is sitting in a pruned tree.
        if skipped_dirs or skipped_binary:
            return "(no matches; binary files and generated dirs were skipped — retry with include_generated=true to search those too)"
        return "(no matches)"

    @tool
    def write_file(project: str, path: str, content: str) -> str:
        """Write (create/overwrite) a text file in a read-write managed project."""
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

    log.info("[fs] %d project(s), %d tool(s), run=%s", len(registry.names()), len(tools), allow_run)
    return tools
