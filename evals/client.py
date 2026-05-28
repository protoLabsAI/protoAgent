"""A2A client for the eval runner.

Drives the running agent over the same JSON-RPC + SSE surface that
real A2A callers use:

- ``agent_card()`` — GET ``/.well-known/agent-card.json``
- ``ask()``        — ``message/send`` + ``tasks/get`` poll
- ``stream()``     — ``message/stream`` SSE
- ``cancel()``     — ``tasks/cancel``

Returns structured ``TaskResult`` objects the runner asserts against.

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
        self.headers = {"Content-Type": "application/json"}
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

    # ── message/send + poll ─────────────────────────────────────────────────

    async def ask(self, prompt: str, *, timeout_s: int = 90, context_id: str | None = None) -> TaskResult:
        """Send + poll until terminal. Returns TaskResult with extracted text.

        ``context_id`` pins the A2A contextId (= the agent's session_id) so
        multiple calls share one session — required for goal-mode cases that
        set a goal on one turn and trigger the loop on the next.
        """
        mid = str(uuid.uuid4())
        params: dict = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
                "messageId": mid,
            }
        }
        if context_id is not None:
            params["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "message/send",
            "params": params,
        }
        start = time.time()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.base_url}/a2a", headers=self.headers, json=payload)
            r.raise_for_status()
            resp = r.json()
            if "error" in resp:
                return TaskResult(task_id="", state="failed", error=str(resp["error"]))
            task_id = resp.get("result", {}).get("id", "")

            deadline = start + timeout_s
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                poll = await client.post(
                    f"{self.base_url}/a2a",
                    headers=self.headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "p",
                        "method": "tasks/get",
                        "params": {"id": task_id},
                    },
                )
                poll.raise_for_status()
                res = poll.json().get("result", {})
                state = (res.get("status") or {}).get("state", "")
                if state in ("completed", "failed", "canceled"):
                    text, usage = _extract(res)
                    return TaskResult(
                        task_id=task_id,
                        state=state,
                        text=text,
                        artifacts=res.get("artifacts", []),
                        usage=usage,
                        duration_ms=int((time.time() - start) * 1000),
                    )
            return TaskResult(
                task_id=task_id, state="timeout",
                duration_ms=int((time.time() - start) * 1000),
            )

    # ── message/stream (SSE) ────────────────────────────────────────────────

    async def stream(self, prompt: str, *, timeout_s: int = 90, context_id: str | None = None) -> tuple[list[dict], TaskResult | None]:
        """Stream a turn over SSE. Returns (event_log, final TaskResult).

        Each event is a dict shaped ``{kind, result}``. Use this to assert
        on the streaming protocol itself (status-update sequence, final
        flag, artifact chunks). Most cases should use ``ask()`` instead.

        ``context_id`` pins the A2A contextId (= session_id) so the turn
        runs in a known session (e.g. one a goal was set on).
        """
        mid = str(uuid.uuid4())
        params: dict = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
                "messageId": mid,
            }
        }
        if context_id is not None:
            params["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "message/stream",
            "params": params,
        }
        events: list[dict] = []
        final: TaskResult | None = None
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
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            events.append({"kind": "raw", "raw": raw})
                            continue
                        result = (data.get("result") or {})
                        kind = result.get("kind", "?")
                        events.append({"kind": kind, "result": result})
                        if kind in ("status-update", "task") and result.get("final"):
                            text, usage = _extract(result)
                            final = TaskResult(
                                task_id=result.get("taskId") or result.get("id", ""),
                                state=(result.get("status") or {}).get("state", "unknown"),
                                text=text,
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

    # ── tasks/cancel ────────────────────────────────────────────────────────

    async def cancel(self, task_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.base_url}/a2a",
                headers=self.headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "c",
                    "method": "tasks/cancel",
                    "params": {"id": task_id},
                },
            )
            return r.json()


def _extract(result: dict) -> tuple[str, dict]:
    """Pull text + cost data out of an A2A result envelope."""
    text_parts: list[str] = []
    usage: dict = {}
    artifacts = result.get("artifacts") or []
    for art in artifacts:
        for p in art.get("parts", []):
            if p.get("kind") == "text" and p.get("text"):
                text_parts.append(p["text"])
            elif p.get("kind") == "data" and isinstance(p.get("data"), dict):
                if "usage" in p["data"]:
                    usage = dict(p["data"]["usage"])
                    if "durationMs" in p["data"]:
                        usage["durationMs"] = p["data"]["durationMs"]
    status = result.get("status") or {}
    msg = status.get("message") or {}
    for p in msg.get("parts") or []:
        if p.get("kind") == "text" and p.get("text"):
            text_parts.append(p["text"])
    return "\n".join(text_parts).strip(), usage
