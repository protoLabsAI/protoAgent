"""Workflow run-state persistence — one JSON file per run (the audit trail).

Every ``_execute`` invocation gets a UUID ``run_id`` and a ``WorkflowRunStore``
that persists the run as ``{runs_dir}/{run_id}.json`` (default runs dir:
``{instance_store}/workflows/.runs/`` — dot-prefixed so the recipe registry's
``*.yaml`` scan never confuses run state with recipes). The file is rewritten
(atomically) on start, after every completed step, and on finish, so run state
survives a server restart and a crash mid-run leaves an honest partial record.

Persistence is **best-effort by design**: a disk hiccup must never fail a
workflow that would otherwise complete, so the mutators swallow ``OSError`` and
keep the run going (state is held in memory and re-written on the next call).

``pending_step`` names the step a ``paused`` run is parked on, awaiting operator
approval (a ``gate: human`` step) — ``None`` for running/terminal runs. ``pause()``
sets it; the run then stays durably resumable (recipe, inputs, completed outputs
are all on the record).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra.paths import atomic_write

log = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PAUSED = "paused"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowRunStore:
    """Persists one workflow run's state; scoped to a runs dir, stateful per run.

    ``start()`` mints the run_id and creates the file; ``step_done()`` /
    ``finish()`` mutate the *current* run (one store instance per execution —
    ``_execute`` constructs a fresh one per invocation, so there's no
    cross-run clobbering). ``load()`` / ``list_runs()`` read any run from disk.
    """

    def __init__(self, runs_dir: Path | str):
        self._dir = Path(runs_dir)
        self._state: dict[str, Any] | None = None

    @property
    def run_id(self) -> str | None:
        """The current run's id (``None`` before ``start()``)."""
        return self._state["run_id"] if self._state else None

    def path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.json"

    def start(self, recipe_name: str, inputs: dict | None = None) -> str:
        """Create the run record (status ``running``) and return its run_id."""
        now = _now()
        self._state = {
            "run_id": str(uuid.uuid4()),
            "recipe_name": recipe_name,
            "inputs": dict(inputs or {}),
            "step_outputs": {},
            "status": STATUS_RUNNING,
            "pending_step": None,
            "created_at": now,
            "updated_at": now,
        }
        self._write()
        return self._state["run_id"]

    def step_done(self, step_id: str, output: str) -> None:
        """Record a completed step's output (for failed steps: the error text)."""
        if self._state is None:
            return
        self._state["step_outputs"][step_id] = output
        self._state["updated_at"] = _now()
        self._write()

    def finish(self, status: str) -> None:
        """Mark the run terminal (``done`` / ``failed``)."""
        if self._state is None:
            return
        self._state["status"] = status
        self._state["updated_at"] = _now()
        self._write()

    def pause(self, pending_step: str, step_outputs: dict[str, str] | None = None) -> str | None:
        """Park the run at ``pending_step`` (status ``paused``) awaiting operator
        approval, recording any completed step outputs. Non-terminal — the run stays
        resumable. Returns the run_id (``None`` before ``start()``)."""
        if self._state is None:
            return None
        if step_outputs:
            self._state["step_outputs"].update(step_outputs)
        self._state["status"] = STATUS_PAUSED
        self._state["pending_step"] = pending_step
        self._state["updated_at"] = _now()
        self._write()
        return self._state["run_id"]

    def load(self, run_id: str) -> dict[str, Any] | None:
        """Read a run's persisted state from disk (``None`` if absent/corrupt)."""
        try:
            data = json.loads(self.path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_runs(self) -> list[str]:
        """Run ids present on disk (unordered beyond filename sort)."""
        if not self._dir.is_dir():
            return []
        return sorted(p.stem for p in self._dir.glob("*.json"))

    def _write(self) -> None:
        try:
            atomic_write(self.path(self._state["run_id"]), json.dumps(self._state, ensure_ascii=False, indent=2))
        except OSError as exc:
            log.warning("[workflows] run-state write failed for %s: %s", self._state["run_id"], exc)
