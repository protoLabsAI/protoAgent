"""`br`/beads backend for the React task-list surface."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from operator_api.paths import resolve_project_path


@dataclass
class BeadsCommandError(RuntimeError):
    command: list[str]
    code: int
    stdout: str
    stderr: str

    def __str__(self) -> str:
        detail = (self.stderr or self.stdout or "").strip()[:400]
        return f"br {self.command[0]} failed (exit {self.code}){': ' + detail if detail else ''}"


class BeadsService:
    """Subprocess wrapper around `br --json`.

    The service intentionally never reads `.beads/beads.db` directly; `br` owns
    locking, JSONL flushes, and store-discovery semantics.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        allowed_dirs: Callable[[], list[str]] | None = None,
    ):
        self.timeout_s = timeout_s
        self._allowed_dirs = allowed_dirs
        self._lock = threading.Lock()

    def status(self, project_path: str) -> dict[str, bool]:
        result = self._run_allow_fail(project_path, ["list", "--json"])
        if result.returncode == 0:
            return {"initialized": True}
        if self._error_code(result.stderr, result.stdout) == "NOT_INITIALIZED":
            return {"initialized": False}
        raise BeadsCommandError(
            ["list", "--json"], result.returncode, result.stdout, result.stderr
        )

    def init(self, project_path: str, prefix: str | None = None) -> dict[str, bool]:
        args = ["init"]
        if prefix:
            args.extend(["--prefix", prefix])
        result = self._run_allow_fail(project_path, args)
        if result.returncode == 0:
            return {"initialized": True, "already_initialized": False}
        if self._error_code(result.stderr, result.stdout) == "ALREADY_INITIALIZED":
            return {"initialized": True, "already_initialized": True}
        raise BeadsCommandError(args, result.returncode, result.stdout, result.stderr)

    def list(self, project_path: str) -> list[dict[str, Any]]:
        raw = self._run(project_path, ["list", "--all", "--json"])
        parsed = self._parse_json(raw)
        issues = parsed if isinstance(parsed, list) else parsed.get("issues", [])
        return [i for i in issues if isinstance(i, dict) and i.get("status") != "tombstone"]

    def create(self, project_path: str, issue: dict[str, Any]) -> dict[str, Any]:
        title = str(issue.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        args = ["create", title, "--json"]
        issue_type = issue.get("type") or issue.get("issue_type")
        if issue_type:
            args.extend(["--type", str(issue_type)])
        if issue.get("priority") is not None:
            args.extend(["--priority", str(issue["priority"])])
        if issue.get("description"):
            args.extend(["--description", str(issue["description"])])
        if issue.get("assignee"):
            args.extend(["--assignee", str(issue["assignee"])])
        return self._first_issue(self._run(project_path, args))

    def update(self, project_path: str, issue_id: str, update: dict[str, Any]) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        args = ["update", issue_id, "--json"]
        for key, flag in (
            ("title", "--title"),
            ("description", "--description"),
            ("status", "--status"),
            ("priority", "--priority"),
            ("type", "--type"),
            ("issue_type", "--type"),
            ("assignee", "--assignee"),
        ):
            if update.get(key) is not None:
                args.extend([flag, str(update[key])])
        return self._first_issue(self._run(project_path, args))

    def close(self, project_path: str, issue_id: str, reason: str | None = None) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        args = ["close", issue_id, "--json"]
        if reason:
            args.extend(["--reason", reason])
        return self._first_issue(self._run(project_path, args))

    def delete(self, project_path: str, issue_id: str) -> dict[str, Any]:
        if not issue_id:
            raise ValueError("issue_id is required")
        parsed = self._parse_json(self._run(project_path, ["delete", issue_id, "--json"]))
        return parsed if isinstance(parsed, dict) else {"deleted": parsed}

    def _run(self, project_path: str, args: list[str]) -> str:
        result = self._run_allow_fail(project_path, args)
        if result.returncode != 0:
            raise BeadsCommandError(args, result.returncode, result.stdout, result.stderr)
        return result.stdout

    def _run_allow_fail(self, project_path: str, args: list[str]) -> subprocess.CompletedProcess:
        allowed = self._allowed_dirs() if self._allowed_dirs is not None else None
        cwd = resolve_project_path(project_path, allowed)
        try:
            with self._lock:
                return subprocess.run(
                    ["br", *args],
                    cwd=str(cwd),
                    env={**os.environ, "RUST_LOG": "error"},
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_s,
                    check=False,
                )
        except FileNotFoundError as exc:
            raise RuntimeError("`br` (beads_rust) is not installed or not on PATH") from exc

    def _parse_json(self, raw: str) -> Any:
        text = raw.strip()
        if not text:
            raise ValueError("br returned empty output")
        return json.loads(text)

    def _first_issue(self, raw: str) -> dict[str, Any]:
        parsed = self._parse_json(raw)
        if isinstance(parsed, list):
            return parsed[0] if parsed else {}
        return parsed

    def _error_code(self, *streams: str) -> str | None:
        for stream in streams:
            try:
                parsed = json.loads(stream.strip())
            except (json.JSONDecodeError, AttributeError):
                continue
            code = parsed.get("error", {}).get("code") if isinstance(parsed, dict) else None
            if code:
                return str(code)
        return None
