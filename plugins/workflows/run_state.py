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

The record also carries what the Studio's live run timeline needs (the run is the
console's polling target while it executes, not just a post-mortem):

* ``steps`` — the recipe's step graph at start time (id / subagent / depends_on /
  gate), so the timeline renders lanes even after the recipe is edited or deleted.
* ``step_meta`` — per-step lifecycle: ``status`` (running / done / failed),
  ``started_at`` / ``finished_at``, and ``seconds`` (the engine's RUNNING time,
  folded in at finish).
* ``output`` / ``failed`` / ``degraded`` — the final envelope, persisted at
  ``finish()`` so history inspection doesn't need the caller's return value.
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

TERMINAL = (STATUS_DONE, STATUS_FAILED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_snapshot(steps: list[dict] | None) -> list[dict]:
    """The graph shape the timeline renders — never the prompts (they can be large
    and template-y; the rendered per-step text lands in ``step_outputs`` instead)."""
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        snap = {
            "id": str(s.get("id", "")),
            "subagent": str(s.get("subagent", "")),
            "depends_on": [str(d) for d in (s.get("depends_on") or [])],
        }
        if s.get("gate") == "human":
            snap["gate"] = "human"
        out.append(snap)
    return out


class WorkflowRunStore:
    """Persists one workflow run's state; scoped to a runs dir, stateful per run.

    ``start()`` mints the run_id and creates the file; ``step_started()`` /
    ``step_done()`` / ``finish()`` mutate the *current* run (one store instance per
    execution — ``_execute`` constructs a fresh one per invocation, so there's no
    cross-run clobbering). ``load()`` / ``list_runs()`` / ``recent()`` read any run
    from disk.
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

    def start(self, recipe_name: str, inputs: dict | None = None, steps: list[dict] | None = None) -> str:
        """Create the run record (status ``running``) and return its run_id."""
        now = _now()
        self._state = {
            "run_id": str(uuid.uuid4()),
            "recipe_name": recipe_name,
            "inputs": dict(inputs or {}),
            "steps": _step_snapshot(steps),
            "step_outputs": {},
            "step_meta": {},
            "status": STATUS_RUNNING,
            "pending_step": None,
            "created_at": now,
            "updated_at": now,
        }
        self._write()
        return self._state["run_id"]

    def step_started(self, step_id: str) -> None:
        """Mark a step dispatched — the live timeline's ``running`` state."""
        if self._state is None:
            return
        self._state["step_meta"][step_id] = {"status": STATUS_RUNNING, "started_at": _now()}
        self._state["updated_at"] = _now()
        self._write()

    def step_done(self, step_id: str, output: str, *, failed: bool = False) -> None:
        """Record a completed step's output (for failed steps: the error text)."""
        if self._state is None:
            return
        self._state["step_outputs"][step_id] = output
        meta = self._state["step_meta"].setdefault(step_id, {})
        meta["status"] = STATUS_FAILED if failed else STATUS_DONE
        meta["finished_at"] = _now()
        self._state["updated_at"] = _now()
        self._write()

    def finish(self, status: str, result: dict | None = None) -> None:
        """Mark the run terminal (``done`` / ``failed``), folding in the engine's
        final envelope when given: final ``output``, ``failed`` / ``degraded`` ids
        (their step_meta flips to failed), and per-step ``timings`` → ``seconds``."""
        if self._state is None:
            return
        self._state["status"] = status
        if result:
            self._state["output"] = str(result.get("output", ""))
            self._state["failed"] = list(result.get("failed") or [])
            self._state["degraded"] = list(result.get("degraded") or [])
            for sid in self._state["failed"]:
                self._state["step_meta"].setdefault(sid, {})["status"] = STATUS_FAILED
            for sid, secs in (result.get("timings") or {}).items():
                self._state["step_meta"].setdefault(sid, {})["seconds"] = secs
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

    def resume(self, run_id: str) -> dict[str, Any] | None:
        """Re-attach this store to an on-disk run and flip it back to ``running``
        (the F3 resume path: a paused run is being continued). Loads the persisted
        state into ``self._state`` — so subsequent ``step_done`` / ``finish`` /
        ``pause`` mutate the SAME record — clears ``pending_step``, and returns the
        loaded state. ``None`` if the run is absent/corrupt (nothing to resume)."""
        state = self.load(run_id)
        if state is None:
            return None
        state["status"] = STATUS_RUNNING
        state["pending_step"] = None
        state["updated_at"] = _now()
        # Records from before the timeline fields existed must still resume cleanly.
        state.setdefault("steps", [])
        state.setdefault("step_meta", {})
        self._state = state
        self._write()
        return state

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

    def paused(self) -> list[dict[str, Any]]:
        """Every ``paused`` run on disk (full state dicts), newest-updated first —
        the operator's "Pending Gates" queue. Corrupt/absent files are skipped."""
        runs = [state for rid in self.list_runs() if (state := self.load(rid)) and state.get("status") == STATUS_PAUSED]
        runs.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return runs

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Run summaries (every status), newest-updated first — the Studio's history
        list. Summaries only: outputs stay behind ``load(run_id)``."""
        runs = [state for rid in self.list_runs() if (state := self.load(rid))]
        runs.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        out = []
        for s in runs[: max(0, limit)]:
            meta = s.get("step_meta") or {}
            out.append(
                {
                    "run_id": s.get("run_id"),
                    "recipe_name": s.get("recipe_name"),
                    "status": s.get("status"),
                    "pending_step": s.get("pending_step"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "steps_total": len(s.get("steps") or []) or len(s.get("step_outputs") or {}),
                    "steps_done": sum(1 for m in meta.values() if m.get("status") == STATUS_DONE)
                    or len(s.get("step_outputs") or {}),
                    "failed": list(s.get("failed") or []),
                }
            )
        return out

    def prune(self, keep: int = 200) -> int:
        """Delete the oldest TERMINAL runs beyond ``keep`` (paused/running records are
        never pruned — they're resumable state, not history). Returns the count removed.
        Called on each run start so ``.runs/`` stops growing unboundedly."""
        if keep <= 0:
            return 0
        terminal = [
            (state.get("updated_at") or "", rid)
            for rid in self.list_runs()
            if (state := self.load(rid)) and state.get("status") in TERMINAL
        ]
        if len(terminal) <= keep:
            return 0
        terminal.sort()  # oldest first
        removed = 0
        for _, rid in terminal[: len(terminal) - keep]:
            try:
                self.path(rid).unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _write(self) -> None:
        try:
            atomic_write(self.path(self._state["run_id"]), json.dumps(self._state, ensure_ascii=False, indent=2))
        except OSError as exc:
            log.warning("[workflows] run-state write failed for %s: %s", self._state["run_id"], exc)
