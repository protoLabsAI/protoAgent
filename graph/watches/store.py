"""WatchStore — per-watch persistence on disk (ADR 0067).

Mirrors ``GoalStore``, but keyed by **watch id** (not session) so an instance holds MANY
concurrent watches. Path resolution mirrors the goal/memory subsystems: ``WATCH_PATH`` env →
``/sandbox/watches`` → ``~/.protoagent/watches``, all ``PROTOAGENT_INSTANCE``-scoped.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from graph.watches.types import Watch

log = logging.getLogger(__name__)


def _publish(topic: str, data: dict) -> None:
    """Best-effort bus push so a console Watches panel can invalidate live. No-op when the
    host hasn't wired a publisher (unit tests); a bus hiccup must never break a write."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass


def _resolve_base() -> Path:
    # Per-instance (ADR 0004/0065), mirroring GoalStore: the default sits at
    # ``instance_root/watches`` so two agents on one machine don't share a watch dir.
    # ``WATCH_PATH`` env overrides verbatim.
    from infra.paths import instance_paths

    candidates = []
    env = os.environ.get("WATCH_PATH", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(instance_paths().store("watches"))
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
    fallback = Path(tempfile.gettempdir()) / "protoagent_watches"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _safe_name(watch_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in watch_id) or "watch"
    # Distinct ids that sanitize to the same string (e.g. "a/b" vs "a_b") must not share a
    # file — disambiguate with a short hash of the RAW id whenever sanitization changed it.
    if safe != watch_id:
        import hashlib

        safe = f"{safe[:48]}-{hashlib.sha1(watch_id.encode()).hexdigest()[:8]}"
    return safe


class WatchStore:
    def __init__(self, base_dir: str | os.PathLike | None = None):
        self._base = Path(base_dir) if base_dir else _resolve_base()
        log.info("[watch] store path: %s", self._base)

    def _path(self, watch_id: str) -> Path:
        return self._base / f"{_safe_name(watch_id)}.json"

    def get(self, watch_id: str) -> Watch | None:
        path = self._path(watch_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return Watch.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            log.warning("[watch] failed to read %s: %s", path, exc)
            return None

    def set(self, watch: Watch) -> None:
        path = self._path(watch.id)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self._base, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(watch.to_dict(), fh, indent=2, default=str)
            os.rename(tmp_path, path)
            tmp_path = None
            _publish("watch.changed", {"id": watch.id})
        except OSError as exc:
            log.error("[watch] write failed for %s: %s", watch.id, exc)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def clear(self, watch_id: str) -> bool:
        path = self._path(watch_id)
        try:
            path.unlink()
            _publish("watch.changed", {"id": watch_id})
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("[watch] clear failed for %s: %s", watch_id, exc)
            return False

    def all(self) -> list[Watch]:
        """Every persisted watch, newest-created first. Unreadable files are skipped+logged."""
        watches: list[Watch] = []
        for path in self._base.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    watches.append(Watch.from_dict(json.load(fh)))
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("[watch] skipping %s: %s", path, exc)
        watches.sort(key=lambda w: getattr(w, "created_at", 0) or 0, reverse=True)
        return watches
