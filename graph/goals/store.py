"""GoalStore — per-session goal persistence on disk.

Goals outlive a single graph run (and the frequent graph rebuilds the server
does on config reload), so state is written to disk keyed by ``session_id``.
Path resolution mirrors the memory/knowledge subsystems: ``GOAL_PATH`` env
(verbatim) → the per-instance ``instance_root/goals`` store → a temp dir as a
last resort if nothing is writable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from graph.goals.types import GoalState

log = logging.getLogger(__name__)


def _publish(topic: str, data: dict) -> None:
    """Best-effort bus push so the console Goals panel can invalidate live on a change
    instead of polling every 5s (#1310). No-op when the host hasn't wired a publisher
    (unit tests / standalone use); a bus hiccup must never break a goal write."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass


def _resolve_base() -> Path:
    # Per-instance (ADR 0004): the default sits at ``instance_root/goals`` so two
    # agents on one machine don't share a goals dir — without isolation, scheduled /
    # activity turns (shared session id "system:activity") collide and goals leak
    # across agents. ``GOAL_PATH`` env overrides verbatim.
    from infra.paths import instance_paths

    candidates = []
    env = os.environ.get("GOAL_PATH", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(instance_paths().store("goals"))
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

    def _plan_path(self, session_id: str) -> Path:
        return self._base / f"{_safe_name(session_id)}.plan.md"

    def read_plan(self, session_id: str) -> str:
        """Read the durable plan artifact for a session (Ralph's fix_plan.md equivalent).
        Returns "" when absent — a fresh goal starts with no plan."""
        path = self._plan_path(session_id)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.warning("[goal] failed to read plan %s: %s", path, exc)
            return ""

    def write_plan(self, session_id: str, content: str) -> None:
        """Write (create/overwrite) the durable plan artifact."""
        path = self._plan_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write mirroring GoalStore.set()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.rename(tmp_path, path)
        except OSError as exc:
            log.warning("[goal] failed to write plan %s: %s", path, exc)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

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
            _publish("goal.changed", {"session_id": state.session_id})
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
            _publish("goal.changed", {"session_id": session_id})
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
