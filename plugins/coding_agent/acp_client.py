"""ACP client — launch a CLI coding agent and drive one session.

protoAgent is the ACP *client*: one ``AcpClient`` owns one agent subprocess and
one session, cached per launch+policy signature so follow-up ``delegate_to``
dispatches continue the same thread (mirrors the A2A peer's sticky ``contextId``). Transport
is JSON-RPC 2.0, newline-delimited, over the child's stdin/stdout. The matching
server side is e.g. ``proto --acp``. Spec: https://agentclientprotocol.com.

Ported from ORBIS's ``acp/client.py`` (the canonical protoLabs ACP client).
ADR 0024.

Surface:
  * handshake: ``initialize`` (honors the negotiated ``protocolVersion`` — closes on
    an unsupported counter) → ``session/load`` the persisted thread when the agent
    advertises ``loadSession`` and we have a saved id, else ``session/new``
  * one turn: ``session/prompt`` → accumulate ``agent_message_chunk`` text as the
    answer; narrate ``tool_call`` titles via ``progress_callback``; surface
    ``agent_thought_chunk`` reasoning via ``thought_callback``
  * lifecycle: ``session/cancel`` on prompt abort, ``session/close`` on teardown
  * auto-allow ``session/request_permission`` (the coding agent self-governs,
    scoped to the session cwd — see ADR 0024); the plugin injects a by-kind policy.
  * ``fs/*`` and ``terminal/*`` are NOT advertised — the coding agent uses its own
    file access, confined to the session ``cwd``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("protoagent.plugins.coding_agent")

ProgressCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[dict], Awaitable[None]]  # structured tool start/end events


def _tool_output_preview(update: dict, limit: int = 300) -> str:
    """Best-effort short text from a tool_call_update's content blocks (for the end card)."""
    out: list[str] = []
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            out.append(inner["text"])
        elif isinstance(block.get("text"), str):
            out.append(block["text"])
    return " ".join(o for o in out if o).strip()[:limit]


def _split_tool_title(title: str) -> tuple[str, str]:
    """Split an ACP tool ``title`` into (label, inline_input). Many agents format the
    title as ``tool_name (… MCP Server): {json args}`` — the inline JSON blows out the
    card header and overflows the chat panel, so peel it into the body. Returns
    ``(label, inline_json)`` where ``inline_json`` is ``""`` when there's none."""
    s = (title or "").strip()
    i = s.find("{")
    if i > 0:
        return s[:i].rstrip().rstrip(":").rstrip(), s[i:].strip()
    return s, ""


def _short_tool_name(title: str) -> str:
    """A compact card label from a (possibly verbose) ACP tool title: drop the inline
    JSON args and a trailing ``(… MCP Server)`` source so the header stays a short
    at-a-glance name (parity with the native runtime's clean tool names). The args +
    source live in the card body instead."""
    label, _ = _split_tool_title(title)
    # Drop a trailing "(… MCP Server)" source suffix only — NOT a legit "(beta)" / "(v2)".
    label = re.sub(r"\s*\([^)]*\b(?:MCP|server)\b[^)]*\)\s*$", "", label, flags=re.IGNORECASE).strip()
    return (label or (title or "").strip() or "tool")[:80]


def _content_text(content) -> str:
    """Text out of a session/update ``content`` field. The ACP spec types it as a
    single ContentBlock (a dict), but agents legitimately send a LIST of blocks —
    and the old ``(content or {}).get("text")`` raised ``AttributeError`` on a list,
    which killed the whole read loop and aborted the turn. Handle dict, list, and
    bare-string shapes."""
    if isinstance(content, dict):
        return content.get("text", "") or ""
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    if isinstance(content, str):
        return content
    return ""


# ACP protocol version protoAgent speaks. Negotiated in `initialize`: the client
# proposes ``PROTOCOL_VERSION`` and the agent echoes it (supported) or counters with
# its latest. We only speak the versions in ``SUPPORTED_PROTOCOL_VERSIONS``; if the
# agent counters with anything else we close the connection (spec: the client SHOULD
# not proceed on an unsupported version) rather than warn-and-continue.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)

# asyncio's StreamReader defaults to a 64 KB line limit; a single newline-delimited
# ACP JSON-RPC message routinely exceeds that (a tool result with a file's contents,
# a large diff, or a resumed session's history). Past the limit, `readline()` raises
# LimitOverrunError, which killed the read loop and aborted the turn mid-build. Give
# the reader generous headroom (per-line buffer ceiling).
_STDOUT_LINE_LIMIT = 32 * 1024 * 1024  # 32 MB

# JSON-RPC error code the agent returns from `session/new` when it has no
# resolved auth (ACP `AUTH_REQUIRED`). The client surfaces an actionable message.
AUTH_REQUIRED = -32000


class AcpError(Exception):
    """Any ACP transport / protocol failure. The caller speaks the message.

    Carries the JSON-RPC error ``code`` when the failure came from an agent
    error response (else ``None``), so callers can special-case e.g.
    ``AUTH_REQUIRED``.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


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
        session_id_path: Path | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.cwd = str(Path(cwd).expanduser())
        self.env = env
        self.name = name
        # Where the session id is persisted so a restart can ``session/load`` the
        # same thread instead of starting fresh (ADR 0024 / #970). ``None`` ⇒ the
        # session lives only as long as this subprocess. The factory derives the
        # path from the client cache key so reattach is keyed to launch+policy+cwd.
        self.session_id_path = session_id_path
        # MCP servers mounted into the ACP session (ADR 0033) — how the coding agent
        # gets protoAgent's operator tools (notes/beads/goals/…) over `session/new`.
        self.mcp_servers = list(mcp_servers or [])
        # Permission resolver: ``(request_params) -> optionId | None`` (None ⇒
        # cancel/deny). Defaults to ``_auto_allow`` — the coding agent self-governs
        # within its workdir. The plugin injects a by-kind policy here (ADR 0024).
        self._permission = permission

        self._proc: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        # Captured from the `initialize` response (was previously discarded).
        self._auth_methods: list[dict] = []
        self._agent_capabilities: dict = {}
        self._protocol_version = PROTOCOL_VERSION  # the negotiated version
        # True only while replaying history during ``session/load`` — suppresses the
        # replayed updates so a silent reattach doesn't re-stream the thread.
        self._loading = False
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
        self._on_thought: ProgressCallback | None = None

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
                # Raise the per-line buffer ceiling — ACP messages exceed the 64 KB
                # default and would otherwise raise LimitOverrunError (kills the turn).
                limit=_STDOUT_LINE_LIMIT,
            )
        except FileNotFoundError as exc:
            raise AcpError(f"agent binary not found: {self.command!r} (is it installed and on PATH?)") from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()
        await self._open_session()
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
        closes ("Event loop is closed"), and the stderr-drain task leaks.

        Sends a best-effort ``session/close`` first — the graceful, spec-aligned
        shutdown (and what matters if an agent ever serves multiple sessions per
        process) before the SIGTERM that actually frees this one-process-per-session."""
        await self._close_session()
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
                # One bad message (an update shape we mishandle, a callback raising)
                # must NOT tear down the session — that aborts the turn mid-build with
                # NO diagnostic (the old behavior: the loop died here, the finally
                # failed the prompt future with "agent exited", and why was lost). Log
                # it and keep reading so the turn survives a single hiccup.
                try:
                    await self._handle(msg)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[acp/%s] error handling message (skipping, turn continues): %.300s",
                        self.name,
                        line,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — surface WHY the loop ended (was silent → undiagnosable)
            logger.exception("[acp/%s] read loop ended on error", self.name)
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
                    err = msg.get("error") or {}
                    if isinstance(err, dict):
                        fut.set_exception(AcpError(str(err.get("message") or err), code=err.get("code")))
                    else:
                        fut.set_exception(AcpError(str(err)))
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
        if self._loading:
            return  # replaying history during session/load — reattaching silently
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                self._answer += text
                await self._emit_text(text)  # stream the delta (token-ish) to the UI
        elif kind == "agent_thought_chunk":
            # The coder's reasoning trace — surface it (never folded into the answer)
            # for parity with the native runtime's thinking stream.
            text = _content_text(update.get("content"))
            if text:
                await self._emit_thought(text)
        elif kind == "tool_call":
            # A tool call STARTED — narrate its title + emit a structured start event so the
            # UI can render a card (parity with the native runtime's tool_start). The card
            # NAME is a short label; the verbose args (structured rawInput, else the title's
            # inline JSON) go into the card BODY so they don't overflow the chat header.
            title = str(update.get("title") or update.get("kind") or "working")
            name = _short_tool_name(title)
            await self._narrate(name)
            raw_input = update.get("rawInput")
            if raw_input not in (None, "", {}, []):
                try:
                    tool_input = json.dumps(raw_input, ensure_ascii=False)
                except (TypeError, ValueError):
                    tool_input = str(raw_input)
            else:
                _, inline = _split_tool_title(title)
                tool_input = inline or str(update.get("kind") or "")
            await self._emit_tool(
                {
                    "phase": "start",
                    "id": str(update.get("toolCallId") or title),
                    "name": name,
                    "input": tool_input,
                }
            )
        elif kind == "tool_call_update":
            # Status transition — emit an end event when it finishes (tool_end card).
            status = str(update.get("status") or "")
            if status in ("completed", "failed"):
                await self._emit_tool(
                    {
                        "phase": "end",
                        "id": str(update.get("toolCallId") or ""),
                        "name": _short_tool_name(str(update.get("title") or "")),
                        "output": _tool_output_preview(update),
                        "status": status,
                    }
                )
        elif kind:
            # plan / current_mode_update / available_commands_update / usage_update —
            # not surfaced yet, but logged so they're visibly dropped, not silent.
            logger.debug("[acp/%s] unhandled session update %r", self.name, kind)

    async def _handle_request(self, msg: dict) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method == "session/request_permission":
            resolver = self._permission or self._auto_allow
            option_id = resolver(msg.get("params") or {})
            outcome = {"outcome": "selected", "optionId": option_id} if option_id else {"outcome": "cancelled"}
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

    async def _emit_thought(self, delta: str) -> None:
        """Surface a reasoning delta. Routes to ``thought_callback`` when wired, else
        falls back to ``progress_callback`` so thoughts are surfaced (not dropped)
        even for callers that only wire narration — the issue's intent."""
        cb = self._on_thought or self._progress
        if cb and delta:
            try:
                await cb(delta)
            except Exception as exc:  # best-effort — thoughts never break a turn
                logger.warning("[acp/%s] thought_callback raised: %s", self.name, exc)

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

    async def _notify_session(self, method: str) -> None:
        """Fire-and-forget a session lifecycle notification (``session/cancel`` on
        abort, ``session/close`` on teardown). Notifications have no id and no
        response, so this writes straight to stdin without a pending future.

        Best-effort by contract — it runs on abort/teardown paths and must never
        raise: no-op when the process is gone or no session is open, and any send
        error is swallowed."""
        proc = self._proc
        if not (proc and proc.returncode is None and proc.stdin and self._session_id):
            return
        try:
            proc.stdin.write(
                (
                    json.dumps({"jsonrpc": "2.0", "method": method, "params": {"sessionId": self._session_id}}) + "\n"
                ).encode()
            )
            await proc.stdin.drain()
        except Exception as exc:  # noqa: BLE001 — abort/teardown path is best-effort
            logger.debug("[acp/%s] %s failed (best-effort): %s", self.name, method, exc)

    async def _cancel_session(self) -> None:
        """Tell the agent to abandon the in-flight turn so a reused session isn't
        left mid-generation. Runs on the prompt abort path (timeout / external
        cancel / transport failure)."""
        await self._notify_session("session/cancel")

    async def _close_session(self) -> None:
        """Tell the agent to release the session before the subprocess is reaped —
        the graceful, spec-aligned counterpart to the SIGTERM in ``close()``."""
        await self._notify_session("session/close")

    # -- handshake -----------------------------------------------------------

    async def _initialize(self) -> None:
        result = (
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
            or {}
        )
        # Keep what the agent told us instead of discarding it: its auth methods
        # (for an actionable auth-required message) and capabilities.
        self._auth_methods = result.get("authMethods") or []
        self._agent_capabilities = result.get("agentCapabilities") or {}
        # Honor the negotiated protocol version (spec): the agent echoes our version
        # if it supports it, else counters with its latest. If that counter is one we
        # don't speak, the client SHOULD close rather than proceed on an incompatible
        # wire. A missing version field is treated leniently as our own (older agents).
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, int):
            if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
                supported = "/".join(str(v) for v in SUPPORTED_PROTOCOL_VERSIONS)
                raise AcpError(
                    f"{self.name} agent negotiated ACP protocol v{negotiated}, but this "
                    f"client only supports v{supported}. Update the coding agent or the "
                    f"client so their ACP versions match."
                )
            self._protocol_version = negotiated

    async def _open_session(self) -> None:
        """Reattach the persisted session when possible, else start a fresh one.

        If a session id was persisted for this launch signature AND the agent
        advertises the ``loadSession`` capability, ``session/load`` reattaches the
        thread (surviving a subprocess crash/restart) instead of losing it to a new
        session. A failed load (expired/unknown id, agent refusal) falls back to a
        fresh ``session/new`` so a stale id never wedges startup."""
        persisted = self._read_persisted_session_id()
        if persisted and self._agent_capabilities.get("loadSession"):
            try:
                await self._load_session(persisted)
                logger.info("[acp/%s] reattached session %s (session/load)", self.name, persisted)
                return
            except AcpError as exc:
                logger.info(
                    "[acp/%s] session/load %s failed (%s) — starting fresh",
                    self.name,
                    persisted,
                    exc,
                )
        await self._new_session()

    async def _load_session(self, session_id: str) -> None:
        """Reattach a persisted session (ACP ``session/load``). The agent replays its
        history via ``session/update`` notifications — suppressed here (``_loading``)
        since we're silently reattaching, not re-streaming the thread — then responds
        ``null``. Caller gates this on the agent's ``loadSession`` capability."""
        self._loading = True
        try:
            await self._request(
                "session/load",
                {"sessionId": session_id, "cwd": self.cwd, "mcpServers": self.mcp_servers},
                timeout=60.0,
            )
        finally:
            self._loading = False
        self._session_id = session_id

    def _read_persisted_session_id(self) -> str | None:
        """The session id saved for this launch signature, or None. Guards on a
        matching ``cwd`` so a stale/copied file can't reattach a foreign workdir."""
        path = self.session_id_path
        if not path:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("cwd") != self.cwd:
            return None
        sid = data.get("sessionId")
        return sid if isinstance(sid, str) and sid else None

    def _persist_session_id(self, session_id: str) -> None:
        """Save the session id (with its ``cwd``) so a later client for the same
        launch signature can ``session/load`` it. Best-effort — never fatal."""
        path = self.session_id_path
        if not (path and session_id):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"sessionId": session_id, "cwd": self.cwd, "command": self.command}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("[acp/%s] could not persist sessionId: %s", self.name, exc)

    async def _new_session(self) -> None:
        try:
            result = await self._request("session/new", {"cwd": self.cwd, "mcpServers": self.mcp_servers}, timeout=30.0)
        except AcpError as exc:
            if exc.code == AUTH_REQUIRED:
                methods = (
                    ", ".join(str(m.get("id") or m.get("name")) for m in self._auth_methods if isinstance(m, dict))
                    or "(none advertised)"
                )
                raise AcpError(
                    f"{self.name} agent requires authentication before a session can "
                    f"start. Configure its credentials in the delegate env (e.g. "
                    f"OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL). Advertised auth "
                    f"methods: {methods}.",
                    code=AUTH_REQUIRED,
                ) from exc
            raise
        self._session_id = (result or {}).get("sessionId")
        if not self._session_id:
            raise AcpError("session/new returned no sessionId")
        self._persist_session_id(self._session_id)

    # -- public: one turn ----------------------------------------------------

    async def prompt(
        self,
        text: str,
        *,
        progress_callback: ProgressCallback | None = None,
        tool_callback: ToolCallback | None = None,
        text_callback: ProgressCallback | None = None,
        thought_callback: ProgressCallback | None = None,
        timeout: float = 600.0,
    ) -> str:
        """Send one user turn; return the agent's accumulated message text.

        Streams ``tool_call`` titles to ``progress_callback`` (text narration), structured
        start/end events to ``tool_callback`` (UI tool cards), answer-text deltas to
        ``text_callback`` (token-ish streaming), and the coder's reasoning deltas to
        ``thought_callback`` (``agent_thought_chunk``; falls back to ``progress_callback``)
        as the agent works. Raises ``AcpError`` on transport/protocol failure.
        """
        await self._ensure_started()
        self._answer = ""
        self._progress = progress_callback
        self._on_tool = tool_callback
        self._on_text = text_callback
        self._on_thought = thought_callback
        try:
            result = await self._request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
            )
        except (AcpError, asyncio.CancelledError):
            # Turn abandoned — internal timeout, external cancel (e.g. an
            # orchestrator's wait_for watchdog), or transport failure. Tell the
            # agent to drain it so the reused session isn't left mid-generation.
            await self._cancel_session()
            raise
        finally:
            self._progress = None
            self._on_tool = None
            self._on_text = None
            self._on_thought = None
        stop = (result or {}).get("stopReason")
        logger.info("[acp/%s] turn complete (stopReason=%s)", self.name, stop)
        return self._answer.strip()
