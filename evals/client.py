"""A2A client for the eval runner.

Drives the running agent over the same JSON-RPC + SSE surface that
real A2A callers use:

- ``agent_card()`` — GET ``/.well-known/agent-card.json``
- ``ask()``        — ``SendMessage`` + ``GetTask`` poll
- ``stream()``     — ``SendStreamingMessage`` SSE
- ``cancel()``     — ``CancelTask``

Returns structured ``TaskResult`` objects the runner asserts against.

This speaks the **A2A 1.0** wire shape that ``a2a-sdk`` (≥1.1) serves:
proto method names (``SendMessage`` etc.), an ``A2A-Version: 1.0``
header (without it the SDK falls back to 0.3 and rejects these
methods), ``role: "ROLE_USER"``, and untyped ``parts: [{"text": …}]``
(no ``kind`` discriminator). See ``tests/test_a2a_handler.py`` for the
canonical request/response shapes.

Auth picks up both surfaces the template exposes (see ``server.py``):

- ``Authorization: Bearer <token>`` — wizard-set / ``A2A_AUTH_TOKEN`` env
- ``X-API-Key: <key>``              — legacy, ``<AGENT>_API_KEY`` env

Both headers are sent when the corresponding env var is set; the
running agent enforces whichever it is configured for.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class TaskResult:
    task_id: str
    state: str                              # completed / failed / canceled / timeout
    text: str = ""                          # extracted user-facing reply
    artifacts: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


def _resolve_auth_env() -> tuple[str, str]:
    """Return (bearer_token, api_key) from env.

    Bearer comes from ``A2A_AUTH_TOKEN`` (the env name the A2A handler
    reads at boot). The API key is named after the agent —
    ``<AGENT_NAME>_API_KEY`` — so a fork named ``quinn`` reads
    ``QUINN_API_KEY``. ``EVAL_API_KEY`` is honored as an explicit
    override so CI doesn't have to know the agent's slug.
    """
    bearer = os.environ.get("A2A_AUTH_TOKEN", "")

    api_key = os.environ.get("EVAL_API_KEY", "")
    if not api_key:
        agent = os.environ.get("AGENT_NAME", "protoagent").upper()
        api_key = os.environ.get(f"{agent}_API_KEY", "")
    return bearer, api_key


class AgentClient:
    """Thin A2A client tied to one agent instance."""

    def __init__(
        self,
        base_url: str | None = None,
        bearer: str | None = None,
        api_key: str | None = None,
    ):
        self.base_url = (
            base_url
            or os.environ.get("EVAL_BASE_URL")
            or os.environ.get("AGENT_BASE_URL")
            or "http://localhost:7870"
        ).rstrip("/")

        env_bearer, env_api_key = _resolve_auth_env()
        token = bearer if bearer is not None else env_bearer
        x_api = api_key if api_key is not None else env_api_key
        # A2A-Version: 1.0 is mandatory — a2a-sdk's dispatcher treats a missing
        # header as 0.3 and then 404s the 1.0 proto methods (SendMessage, …).
        self.headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        if x_api:
            self.headers["X-API-Key"] = x_api

    # ── Agent card ──────────────────────────────────────────────────────────

    async def agent_card(self) -> dict:
        """Fetch the agent card.

        The template serves both ``/.well-known/agent-card.json`` (modern)
        and ``/.well-known/agent.json`` (legacy). We try the modern path
        first; fall back to the legacy path so this works against forks
        that disabled one or the other.
        """
        async with httpx.AsyncClient(timeout=10) as client:
            for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                r = await client.get(f"{self.base_url}{path}")
                if r.status_code == 200:
                    return r.json()
            r.raise_for_status()  # surface the last error
            return {}

    async def health(self) -> dict:
        """GET ``/healthz`` → ``{ok, graph_compiled, setup_complete, ui, model}``.

        The eval runner reads ``model`` from here to tag the report with the
        model under test (no guessing which model produced a run)."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base_url}/healthz")
            return r.json()

    # ── workflows ─────────────────────────────────────────────────────────────

    async def run_workflow(self, name: str, inputs: dict, *, timeout_s: int = 300) -> dict:
        """POST ``/api/workflows/{name}/run`` → the workflow result dict.

        Used by ``kind: "workflow"`` eval cases to drive a recipe (e.g.
        ``deep-research``) end-to-end and assert on its synthesized output."""
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                f"{self.base_url}/api/workflows/{name}/run",
                headers=self.headers,
                json={"inputs": inputs},
            )
            r.raise_for_status()
            return r.json()

    # ── message/send + poll ─────────────────────────────────────────────────

    async def ask(self, prompt: str, *, timeout_s: int = 90, context_id: str | None = None) -> TaskResult:
        """Send + poll until terminal. Returns TaskResult with extracted text.

        ``context_id`` pins the A2A contextId (= the agent's session_id) so
        multiple calls share one session — required for goal-mode cases that
        set a goal on one turn and trigger the loop on the next.
        """
        mid = str(uuid.uuid4())
        message: dict = {
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
            "messageId": mid,
        }
        # contextId is a field of Message in 1.0 — SendMessageRequest itself only
        # has {tenant, message, configuration, metadata}, so putting it at params
        # level is a -32602.
        if context_id is not None:
            message["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "SendMessage",
            "params": {"message": message},
        }
        start = time.time()

        def _finish(res: dict, task_id: str) -> TaskResult:
            text, usage = _extract(res)
            return TaskResult(
                task_id=task_id,
                state=_norm_state((res.get("status") or {}).get("state", "")),
                text=text,
                artifacts=res.get("artifacts", []),
                usage=usage,
                duration_ms=int((time.time() - start) * 1000),
            )

        # Non-streaming SendMessage *blocks* until the task is terminal (a2a-sdk
        # 1.1), so the POST itself must hold the whole turn budget — a fixed 30s
        # would ReadTimeout on any slow turn (web_search, subagents) even though
        # the case allows longer. (The 0.3 message/send returned immediately and
        # this client polled; 1.0 collapsed that into one blocking call.)
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                r = await client.post(f"{self.base_url}/a2a", headers=self.headers, json=payload)
            except httpx.TimeoutException:
                return TaskResult(
                    task_id="", state="timeout",
                    duration_ms=int((time.time() - start) * 1000),
                )
            r.raise_for_status()
            resp = r.json()
            if "error" in resp:
                return TaskResult(task_id="", state="failed", error=str(resp["error"]))
            # SendMessage wraps the task in result.task (vs GetTask, which returns
            # the task directly at result). Non-streaming SendMessage blocks until
            # terminal, so the first response is usually already complete.
            task = resp.get("result", {}).get("task", {})
            task_id = task.get("id", "")
            if _is_terminal((task.get("status") or {}).get("state", "")):
                return _finish(task, task_id)

            deadline = start + timeout_s
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                poll = await client.post(
                    f"{self.base_url}/a2a",
                    headers=self.headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "p",
                        "method": "GetTask",
                        "params": {"id": task_id},
                    },
                )
                poll.raise_for_status()
                res = poll.json().get("result", {})
                if _is_terminal((res.get("status") or {}).get("state", "")):
                    return _finish(res, task_id)
            return TaskResult(
                task_id=task_id, state="timeout",
                duration_ms=int((time.time() - start) * 1000),
            )

    # ── SendStreamingMessage (SSE) ──────────────────────────────────────────

    async def stream(self, prompt: str, *, timeout_s: int = 90, context_id: str | None = None) -> tuple[list[dict], TaskResult | None]:
        """Stream a turn over SSE. Returns (event_log, final TaskResult).

        Each SSE ``data:`` frame is an A2A 1.0 oneof under ``result`` — one of
        ``task`` (initial snapshot), ``statusUpdate``, ``artifactUpdate``, or
        ``message``. We log each as ``{kind, result}`` (``kind`` is the oneof
        field name), accumulate artifact parts, and finalize when a status
        frame is ``final`` or terminal. Use this to assert on the streaming
        protocol itself; most cases should use ``ask()`` instead.

        ``context_id`` pins the A2A contextId (= session_id) so the turn
        runs in a known session (e.g. one a goal was set on).
        """
        mid = str(uuid.uuid4())
        message: dict = {
            "role": "ROLE_USER",
            "parts": [{"text": prompt}],
            "messageId": mid,
        }
        if context_id is not None:  # see ask(): contextId lives inside the message
            message["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }
        events: list[dict] = []
        final: TaskResult | None = None
        artifacts: list[dict] = []  # accumulated across artifactUpdate frames
        task_id = ""
        start = time.time()
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            async with client.stream(
                "POST", f"{self.base_url}/a2a", headers=self.headers, json=payload
            ) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    return events, TaskResult(
                        task_id="", state="failed",
                        error=f"HTTP {r.status_code}: {body.decode()[:300]}",
                    )
                async for line in r.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        events.append({"kind": "raw", "raw": raw})
                        continue
                    result = data.get("result") or {}
                    kind = next(iter(result), "?") if result else "?"
                    payload_obj = result.get(kind, {}) if kind != "?" else {}
                    events.append({"kind": kind, "result": payload_obj})

                    if kind == "task":
                        task_id = payload_obj.get("id", "") or task_id
                        artifacts = payload_obj.get("artifacts") or artifacts
                    elif kind == "artifactUpdate":
                        task_id = payload_obj.get("taskId", "") or task_id
                        art = payload_obj.get("artifact")
                        if art:
                            artifacts.append(art)
                    elif kind == "statusUpdate":
                        task_id = payload_obj.get("taskId", "") or task_id
                        status = payload_obj.get("status") or {}
                        state = status.get("state", "")
                        if payload_obj.get("final") or _is_terminal(state):
                            text, usage = _extract(
                                {"artifacts": artifacts, "status": status}
                            )
                            final = TaskResult(
                                task_id=task_id,
                                state=_norm_state(state) or "unknown",
                                text=text,
                                artifacts=artifacts,
                                usage=usage,
                                duration_ms=int((time.time() - start) * 1000),
                            )
                            break
        return events, final

    # ── goal mode ─────────────────────────────────────────────────────────────

    async def get_goal(self, session_id: str) -> dict:
        """GET ``/api/goal/{session_id}`` → ``{enabled, goal}`` (goal is the
        full persisted state dict, or None when no goal is set)."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base_url}/api/goal/{session_id}", headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def clear_goal(self, session_id: str) -> dict:
        """DELETE ``/api/goal/{session_id}`` → ``{enabled, cleared}``."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(
                "DELETE", f"{self.base_url}/api/goal/{session_id}", headers=self.headers,
            )
            r.raise_for_status()
            return r.json()

    # ── CancelTask ──────────────────────────────────────────────────────────

    async def cancel(self, task_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.base_url}/a2a",
                headers=self.headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "c",
                    "method": "CancelTask",
                    "params": {"id": task_id},
                },
            )
            return r.json()


def _norm_state(state: str) -> str:
    """``TASK_STATE_COMPLETED`` → ``completed``; pass through already-lowercased
    legacy states unchanged. The runner asserts on the lowercase names."""
    if state.startswith("TASK_STATE_"):
        return state[len("TASK_STATE_"):].lower()
    return state


def _is_terminal(state: str) -> bool:
    """A task in one of these states won't change on further polling. Mirrors
    ``tests/test_a2a_handler.py::_poll_terminal`` (input_required parks the
    task — it's terminal for polling purposes)."""
    return _norm_state(state) in ("completed", "failed", "canceled", "input_required")


def _extract(result: dict) -> tuple[str, dict]:
    """Pull text + cost data out of an A2A result envelope.

    Tolerant of both the A2A 1.0 part shape (untyped — a part carries a
    ``text`` or ``data`` field directly) and the legacy 0.x shape (parts
    tagged with ``kind``)."""
    text_parts: list[str] = []
    usage: dict = {}

    def _scan(parts: list[dict] | None) -> None:
        nonlocal usage
        for p in parts or []:
            if p.get("text"):
                text_parts.append(p["text"])
            elif isinstance(p.get("data"), dict) and "usage" in p["data"]:
                usage = dict(p["data"]["usage"])
                if "durationMs" in p["data"]:
                    usage["durationMs"] = p["data"]["durationMs"]

    for art in result.get("artifacts") or []:
        _scan(art.get("parts"))
    status = result.get("status") or {}
    msg = status.get("message") or {}
    _scan(msg.get("parts"))
    return "\n".join(text_parts).strip(), usage
