"""GoalStore — per-session goal persistence on disk.

Goals outlive a single graph run (and the frequent graph rebuilds the server
does on config reload), so state is written to disk keyed by ``session_id``.
Path resolution mirrors the memory/knowledge subsystems: ``GOAL_PATH`` env →
``/sandbox/goals`` → ``~/.protoagent/goals`` fallback when the sandbox path
isn't writable (e.g. running locally without ``/sandbox``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from graph.goals.types import GoalState

log = logging.getLogger(__name__)


def _resolve_base() -> Path:
    candidates = []
    env = os.environ.get("GOAL_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/sandbox/goals"))
    candidates.append(Path.home() / ".protoagent" / "goals")
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            # confirm writable
            probe = path / ".write_probe"
            probe.touch()
            probe.unlink()
            return path
        except OSError:
            continue
    # Last resort: a temp dir (keeps the server alive even if nothing is writable).
    fallback = Path(tempfile.gettempdir()) / "protoagent_goals"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _safe_name(session_id: str) -> str:
    # session_id is operator/peer-supplied; keep it filesystem-safe.
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id) or "default"


class GoalStore:
    def __init__(self, base_dir: str | os.PathLike | None = None):
        self._base = Path(base_dir) if base_dir else _resolve_base()
        log.info("[goal] store path: %s", self._base)

    def _path(self, session_id: str) -> Path:
        return self._base / f"{_safe_name(session_id)}.json"

    def get(self, session_id: str) -> GoalState | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return GoalState.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            log.warning("[goal] failed to read %s: %s", path, exc)
            return None

    def set(self, state: GoalState) -> None:
        path = self._path(state.session_id)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self._base, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(state.to_dict(), fh, indent=2, default=str)
            os.rename(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            log.error("[goal] write failed for session %s: %s", state.session_id, exc)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # Update is identical to set (whole-record write); kept as an alias for
    # call-site readability.
    update = set

    def clear(self, session_id: str) -> bool:
        path = self._path(session_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("[goal] clear failed for %s: %s", session_id, exc)
            return False

    def all(self) -> list[GoalState]:
        """Every persisted goal across sessions, newest-started first.

        Best-effort: unreadable/corrupt files are skipped and logged. Used by
        the console's Goals panel to list goals beyond the current session.
        """
        states: list[GoalState] = []
        for path in self._base.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    states.append(GoalState.from_dict(json.load(fh)))
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("[goal] skipping %s: %s", path, exc)
        states.sort(key=lambda s: getattr(s, "started_at", 0) or 0, reverse=True)
        return states
