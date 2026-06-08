"""ACP client — launch a CLI coding agent and drive one session.

protoAgent is the ACP *client*: one ``AcpClient`` owns one agent subprocess and
one session, cached per launch+policy signature so follow-up ``delegate_to``
dispatches continue the same thread (mirrors the A2A peer's sticky ``contextId``). Transport
is JSON-RPC 2.0, newline-delimited, over the child's stdin/stdout. The matching
server side is e.g. ``proto --acp``. Spec: https://agentclientprotocol.com.

Ported from ORBIS's ``acp/client.py`` (the canonical protoLabs ACP client).
ADR 0024.

PR1 scope (the thin vertical):
  * handshake: ``initialize`` → ``session/new`` (cwd = the agent's config workdir)
  * one turn: ``session/prompt`` → accumulate ``agent_message_chunk`` text as the
    answer; narrate ``tool_call`` titles via ``progress_callback`` ("Editing
    app.py", "Running pytest")
  * auto-allow ``session/request_permission`` (the coding agent self-governs,
    scoped to the session cwd — see ADR 0024). Policy + HITL gating land next.
  * ``fs/*`` and ``terminal/*`` are NOT advertised — the coding agent uses its own
    file access, confined to the session ``cwd``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("protoagent.plugins.coding_agent")

ProgressCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[dict], Awaitable[None]]  # structured tool start/end events


def _tool_output_preview(update: dict, limit: int = 300) -> str:
    """Best-effort short text from a tool_call_update's content blocks (for the end card)."""
    out: list[str] = []
    for block in (update.get("content") or []):
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            out.append(inner["text"])
        elif isinstance(block.get("text"), str):
            out.append(block["text"])
    return " ".join(o for o in out if o).strip()[:limit]

# ACP protocol version protoAgent speaks. Negotiated in `initialize`.
PROTOCOL_VERSION = 1


class AcpError(Exception):
    """Any ACP transport / protocol failure. The caller speaks the message."""


class AcpClient:
    """Drive a single ACP agent subprocess + session.

    Construct once per configured agent and reuse: the process + session persist
    across turns. Not safe for concurrent prompts on one instance (a session is a
    single conversation); callers serialize turns with a per-agent lock.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        name: str = "acp",
        permission: Callable[[dict], str | None] | None = None,
        mcp_servers: list[dict] | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.cwd = str(Path(cwd).expanduser())
        self.env = env
        self.name = name
        # MCP servers mounted into the ACP session (ADR 0033) — how the coding agent
        # gets protoAgent's operator tools (notes/beads/goals/…) over `session/new`.
        self.mcp_servers = list(mcp_servers or [])
        # Permission resolver: ``(request_params) -> optionId | None`` (None ⇒
        # cancel/deny). Defaults to ``_auto_allow`` — the coding agent self-governs
        # within its workdir. The plugin injects a by-kind policy here (ADR 0024).
        self._permission = permission

        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()

        # Per-turn state (one turn at a time).
        self._answer = ""
        self._progress: ProgressCallback | None = None
        self._on_tool: ToolCallback | None = None
        self._on_text: ProgressCallback | None = None

    # -- lifecycle -----------------------------------------------------------

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            await self._start()

    async def _start(self) -> None:
        if not Path(self.cwd).is_dir():
            raise AcpError(f"workdir does not exist: {self.cwd}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **(self.env or {})},
            )
        except FileNotFoundError as exc:
            raise AcpError(
                f"agent binary not found: {self.command!r} (is it installed and on PATH?)"
            ) from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()
        await self._new_session()
        logger.info(
            "[acp/%s] up (pid=%s, session=%s, cwd=%s)",
            self.name,
            self._proc.pid,
            self._session_id,
            self.cwd,
        )

    async def close(self) -> None:
        """Cancel the I/O tasks and reap the subprocess. Crucially this ``await``s
        ``proc.wait()`` so the child is reaped *while the loop is alive* — without
        it the subprocess transport lingers and its ``__del__`` fires after the loop
        closes ("Event loop is closed"), and the stderr-drain task leaks."""
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        proc = self._proc
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
        # Close the subprocess transport too, so its pipe transports don't linger to
        # a post-loop-close GC — reaping the process (above) leaves the stdin write-
        # pipe transport open, whose __del__ then fires "Event loop is closed".
        transport = getattr(proc, "_transport", None) if proc else None
        if transport is not None:
            transport.close()

    # -- I/O loops -----------------------------------------------------------

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for raw in self._proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                logger.debug("[acp/%s/stderr] %s", self.name, line)

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[acp/%s] non-JSON line: %.200s", self.name, line)
                    continue
                await self._handle(msg)
        except asyncio.CancelledError:
            raise
        finally:
            # Fail any in-flight requests if the process dies mid-turn.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError(f"{self.name} agent exited"))
            self._pending.clear()

    async def _handle(self, msg: dict) -> None:
        # 1) Response to one of our outbound requests.
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return
        method = msg.get("method")
        # 2) Inbound request from the agent (has id) — we must respond.
        if method and "id" in msg:
            await self._handle_request(msg)
            return
        # 3) Notification (no id).
        if method == "session/update":
            await self._handle_update(msg.get("params") or {})

    # -- inbound updates + requests -----------------------------------------

    async def _handle_update(self, params: dict) -> None:
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text", "")
            if text:
                self._answer += text
                await self._emit_text(text)   # stream the delta (token-ish) to the UI
        elif kind == "tool_call":
            # A tool call STARTED — narrate its title + emit a structured start event so the
            # UI can render a card (parity with the native runtime's tool_start).
            title = update.get("title") or update.get("kind") or "working"
            await self._narrate(str(title))
            await self._emit_tool({
                "phase": "start",
                "id": str(update.get("toolCallId") or title),
                "name": str(title),
                "input": str(update.get("kind") or ""),
            })
        elif kind == "tool_call_update":
            # Status transition — emit an end event when it finishes (tool_end card).
            status = str(update.get("status") or "")
            if status in ("completed", "failed"):
                await self._emit_tool({
                    "phase": "end",
                    "id": str(update.get("toolCallId") or ""),
                    "name": str(update.get("title") or ""),
                    "output": _tool_output_preview(update),
                    "status": status,
                })

    async def _handle_request(self, msg: dict) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method == "session/request_permission":
            resolver = self._permission or self._auto_allow
            option_id = resolver(msg.get("params") or {})
            outcome = (
                {"outcome": "selected", "optionId": option_id}
                if option_id
                else {"outcome": "cancelled"}
            )
            await self._respond(rid, {"outcome": outcome})
        else:
            # We didn't advertise fs/terminal; decline anything else cleanly so
            # the agent falls back to its own capabilities instead of hanging.
            await self._respond_error(rid, -32601, f"method not supported: {method}")

    @staticmethod
    def _auto_allow(params: dict) -> str | None:
        """Default permission policy: pick the first 'allow' option (else the
        first option). The plugin's by-kind policy (ADR 0024) overrides this."""
        options = params.get("options") or []
        for opt in options:
            if str(opt.get("kind", "")).startswith("allow"):
                return opt.get("optionId")
        return options[0].get("optionId") if options else None

    async def _narrate(self, text: str) -> None:
        if self._progress and text:
            try:
                await self._progress(text)
            except Exception as exc:  # progress is best-effort
                logger.warning("[acp/%s] progress_callback raised: %s", self.name, exc)

    async def _emit_tool(self, event: dict) -> None:
        if self._on_tool:
            try:
                await self._on_tool(event)
            except Exception as exc:  # best-effort — tool cards never break a turn
                logger.warning("[acp/%s] tool_callback raised: %s", self.name, exc)

    async def _emit_text(self, delta: str) -> None:
        if self._on_text and delta:
            try:
                await self._on_text(delta)
            except Exception as exc:  # best-effort — streaming never breaks a turn
                logger.warning("[acp/%s] text_callback raised: %s", self.name, exc)

    # -- JSON-RPC primitives -------------------------------------------------

    async def _send(self, obj: dict) -> None:
        if not (self._proc and self._proc.stdin):
            raise AcpError("agent not started")
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict, *, timeout: float = 120.0):
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} timed out after {timeout}s") from exc

    async def _respond(self, rid, result: dict) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _respond_error(self, rid, code: int, message: str) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # -- handshake -----------------------------------------------------------

    async def _initialize(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # PR1: no client-served fs/terminal — the coding agent uses its own,
                # confined to the session cwd.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
            timeout=30.0,
        )

    async def _new_session(self) -> None:
        result = await self._request(
            "session/new", {"cwd": self.cwd, "mcpServers": self.mcp_servers}, timeout=30.0
        )
        self._session_id = (result or {}).get("sessionId")
        if not self._session_id:
            raise AcpError("session/new returned no sessionId")

    # -- public: one turn ----------------------------------------------------

    async def prompt(
        self,
        text: str,
        *,
        progress_callback: ProgressCallback | None = None,
        tool_callback: ToolCallback | None = None,
        text_callback: ProgressCallback | None = None,
        timeout: float = 600.0,
    ) -> str:
        """Send one user turn; return the agent's accumulated message text.

        Streams ``tool_call`` titles to ``progress_callback`` (text narration), structured
        start/end events to ``tool_callback`` (UI tool cards), and answer-text deltas to
        ``text_callback`` (token-ish streaming) as the agent works. Raises ``AcpError`` on
        transport/protocol failure.
        """
        await self._ensure_started()
        self._answer = ""
        self._progress = progress_callback
        self._on_tool = tool_callback
        self._on_text = text_callback
        try:
            result = await self._request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
            )
        finally:
            self._progress = None
            self._on_tool = None
            self._on_text = None
        stop = (result or {}).get("stopReason")
        logger.info("[acp/%s] turn complete (stopReason=%s)", self.name, stop)
        return self._answer.strip()
