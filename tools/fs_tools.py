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
- ``run_command`` is the dual-use power tool (like ``execute_code``): fenced
  ``cwd``, but arbitrary argv — gated behind ``filesystem.allow_run``.
- Returns clean error strings (never raises into the runner); ``AuditMiddleware``
  records every call; given to subagents only when their allowlist names it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import ToolException, tool

from tools.shell import run_command as _shell_run

log = logging.getLogger("protoagent.fs")

_MAX_READ_CHARS = 50_000
_MAX_LIST = 400
_MAX_MATCHES = 200


@dataclass
class Project:
    name: str
    root: Path
    write: bool = False


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


def _registry_from_config(config) -> ProjectRegistry:
    projects: list[Project] = []
    # Explicit projects, or the default workspace dir (created) when none are
    # configured — the on-by-default fenced workspace.
    entries = (
        config.effective_filesystem_projects(create=True)
        if hasattr(config, "effective_filesystem_projects")
        else (getattr(config, "filesystem_projects", []) or [])
    )
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
        projects.append(Project(name=name, root=root, write=bool(entry.get("write", False))))
    return ProjectRegistry(projects)


def build_fs_tools(config) -> list:
    """Build the fenced filesystem tools from config. Empty list when no valid
    projects are registered (so the primitive is inert by default)."""
    registry = _registry_from_config(config)
    if not registry.names():
        log.info("[fs] filesystem enabled but no valid projects registered — no tools")
        return []
    allow_run = bool(getattr(config, "filesystem_allow_run", False))
    # run_command is unsandboxed (arbitrary argv as the server user), so by
    # default each invocation is gated behind a HITL approval (the operator sees
    # the command + approves/denies). Forks can disable the gate (e.g. inside a
    # hardened container, or for a trusted autonomous deploy).
    run_requires_approval = bool(getattr(config, "filesystem_run_requires_approval", True))

    @tool
    def list_projects() -> str:
        """List the project workspaces you manage (name, path, read-only vs read-write)."""
        lines = ["Managed projects:"]
        for name in registry.names():
            p = registry.get(name)
            lines.append(f"- {name}  [{'rw' if p.write else 'ro'}]  {p.root}")
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
            text = target.read_text(encoding="utf-8", errors="replace")
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
        rels = [str(p.relative_to(root)) for p in matches[:_MAX_MATCHES]]
        more = f"\n… (+{len(matches) - _MAX_MATCHES} more)" if len(matches) > _MAX_MATCHES else ""
        return "\n".join(rels) + more if rels else "(no matches)"

    @tool
    def search_files(project: str, query: str, path: str = ".") -> str:
        """Substring-search files under a managed project path; returns file:line matches."""
        try:
            base = registry.resolve(project, path)
        except ValueError as exc:
            return f"Error: {exc}"
        root = registry.resolve(project, ".")
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        hits: list[str] = []
        for f in files:
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        hits.append(f"{f.relative_to(root)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= _MAX_MATCHES:
                            return "\n".join(hits) + "\n… (more matches; narrow the search)"
            except OSError:
                continue
        return "\n".join(hits) if hits else "(no matches)"

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
            target.write_text(content, encoding="utf-8")
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
        text = target.read_text(encoding="utf-8", errors="replace")
        if old not in text:
            return f"Error: `old` not found in {path}."
        if text.count(old) > 1:
            return f"Error: `old` is not unique in {path} ({text.count(old)} matches) — add context."
        try:
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as exc:
            return f"Error: cannot write {path}: {exc}"
        return f"Edited {path}."

    tools = [list_projects, list_dir, read_file, find_files, search_files, write_file, edit_file]

    if allow_run:

        @tool
        async def run_command(project: str, command: str, timeout: float = 60.0) -> str:
            """Run a shell command inside a managed project's directory (fenced cwd).

            Powerful + dual-use (like execute_code) — use it for read-only
            inspection (`git status`, `gh pr list`, `br list`) and only mutate in
            read-write projects. Runs via ``/bin/sh -c``, so shell operators
            (``&&``, ``|``, ``>``, ``$(…)``) work.
            """
            try:
                root = registry.resolve(project, ".")
            except ValueError as exc:
                return f"Error: {exc}"
            if not command.strip():
                return "Error: empty command."
            # HITL approval gate (Sprint A): pause for the operator to approve
            # the command before it runs. interrupt() re-runs this fn from the
            # top on resume (the validation above is idempotent) and returns the
            # operator's decision. Denied → don't run.
            if run_requires_approval:
                from langgraph.types import interrupt

                decision = interrupt(
                    {
                        "kind": "approval",
                        "title": "Approve shell command?",
                        "detail": command,
                        "project": project,
                    }
                )
                if not _approved(decision):
                    # Raise (not return) so the ToolNode stamps the ToolMessage
                    # status="error": the chat card then renders the declined action
                    # as a failure (red X) instead of a green "done" with decline text.
                    raise ToolException(f"Command declined by the operator — not run: {command!r}")
            res = await _shell_run(["/bin/sh", "-c", command], cwd=str(root), timeout=timeout)
            if res.error:
                raise ToolException(res.error)
            body = res.stdout or "(no output)"
            if res.stderr:
                body += f"\n[stderr]\n{res.stderr}"
            return body[:_MAX_READ_CHARS] + (f"\n(exit {res.returncode})" if res.returncode else "")

        tools.append(run_command)

    log.info("[fs] %d project(s), %d tool(s), run=%s", len(registry.names()), len(tools), allow_run)
    return tools
