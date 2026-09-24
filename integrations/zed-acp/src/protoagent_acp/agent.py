"""The ACP agent: Zed (or any ACP client) on stdio ↔ a running protoAgent over A2A.

Mapping:

=====================================  =========================================================
ACP                                    A2A (protoAgent ``/a2a``)
=====================================  =========================================================
``initialize``                         nothing (reports capabilities + ``authMethods``)
``authenticate``                       re-reads credentials, ``GetTask`` probe
``session/new``                        a fresh ``contextId`` (``chat-zed-…``, so the conversation
                                       also shows up in the protoAgent console's chat list)
``session/prompt``                     ``SendStreamingMessage`` on that contextId
``session/cancel``                     ``CancelTask`` on the running task
``agent_message_chunk``                artifact-update text (append), terminal REPLACE de-duplicated
``agent_thought_chunk``                reasoning-v1 DataPart
``tool_call`` / ``tool_call_update``   tool-call-v1 metadata (started → completed/failed)
``session/request_permission``         a hitl-v1 ``approval`` park → resume ``approved``/``denied``
=====================================  =========================================================

A parked *question* or *form* has no ACP equivalent the shim can render faithfully, so it is
shown as text and the turn ends; the operator's NEXT prompt in that session is sent as the
answer (``metadata.hitl_resume``), which is exactly how the console resumes a parked task.

File writes stay protoAgent's (ADR 0111 D4): the shim never calls ``fs/write_text_file``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, NoReturn

from acp import (
    PROTOCOL_VERSION,
    RequestError,
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AuthEnvVar,
    EnvVarAuthMethod,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    TerminalAuthMethod,
    ToolCallLocation,
    ToolCallUpdate,
    Usage,
)

from . import __version__
from . import tools as toolmap
from .a2a import (
    A2AClient,
    A2AError,
    A2AUnauthorized,
    ReasoningEvent,
    StateEvent,
    TextEvent,
    ToolEvent,
    UsageEvent,
    decode_frame,
    frame_context_id,
    task_snapshot,
)
from .roots import RootMap

log = logging.getLogger(__name__)

AUTH_METHODS = [
    # The registry requires at least one `agent` or `terminal` method. Terminal: Zed runs
    # `protoagent-acp login` in a terminal; the token lands in the credentials file.
    TerminalAuthMethod(
        type="terminal",
        id="protoagent-login",
        name="Sign in to a protoAgent instance",
        description="Enter the instance URL and its bearer token once; stored 0600 for later launches.",
        args=["login"],
    ),
    EnvVarAuthMethod(
        type="env_var",
        id="protoagent-token-env",
        name="protoAgent token from the environment",
        description="Set PROTOAGENT_TOKEN (and PROTOAGENT_URL) in the agent_servers env.",
        vars=[AuthEnvVar(name="PROTOAGENT_TOKEN"), AuthEnvVar(name="PROTOAGENT_URL", secret=False, optional=True)],
    ),
]


@dataclass
class Session:
    id: str  # ACP sessionId == A2A contextId
    cwd: str
    first_prompt: bool = True
    task_id: str | None = None  # the A2A task currently streaming
    parked_task_id: str | None = None  # an input-required task waiting for the next prompt
    runner: asyncio.Task | None = None
    cancelled: bool = False
    spoke: bool = False  # any answer text sent this prompt (for separators)
    announced: set[str] = field(default_factory=set)
    args: dict[str, dict] = field(default_factory=dict)


def _new_context_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


_HTTP_CODE = re.compile(r"Error code: (\d{3})")
_ERR_MESSAGE = re.compile(r"""['"]message['"]\s*:\s*['"]([^'"]+)['"]""")
_ERR_TYPE = re.compile(r"""['"](?:type|code)['"]\s*:\s*['"]([^'"]+)['"]""")


def friendly_error(raw: str) -> str:
    """``Error code: 429 - {'error': {'type': 'usage_limit_reached', 'message': 'The usage
    limit has been reached', …}}`` → ``The usage limit has been reached (HTTP 429,
    usage_limit_reached)``. Anything else passes through (trimmed)."""
    raw = (raw or "").strip()
    msg = _ERR_MESSAGE.search(raw)
    if not msg:
        return raw[:500] or "unknown error"
    bits = []
    if code := _HTTP_CODE.search(raw):
        bits.append(f"HTTP {code.group(1)}")
    if kind := _ERR_TYPE.search(raw):
        bits.append(kind.group(1))
    return msg.group(1) + (f" ({', '.join(bits)})" if bits else "")


def prompt_text(blocks: list[Any]) -> str:
    """ACP prompt content → one A2A text part. Zed sends ``resource_link`` for an
    @-mentioned file and an embedded ``resource`` when it inlines the contents."""
    out: list[str] = []
    for b in blocks or []:
        t = getattr(b, "type", None)
        if t == "text":
            out.append(b.text)
        elif t == "resource_link":
            out.append(f"[@{getattr(b, 'name', '') or b.uri}]({b.uri})")
        elif t == "resource":
            res = b.resource
            body = getattr(res, "text", None)
            if body is not None:
                out.append(f"\n<file uri=\"{res.uri}\">\n{body}\n</file>\n")
            else:
                out.append(f"[attached {res.uri}]")
        elif t in ("image", "audio"):
            out.append(f"[{t} attachment omitted — not forwarded by protoagent-acp yet]")
    return "\n".join(s for s in out if s)


class ProtoAgentACP:
    """Implements the ``acp.Agent`` protocol."""

    def __init__(
        self,
        client: A2AClient,
        roots: RootMap,
        *,
        context_prefix: str = "chat-zed",
        reload_credentials: Any = None,
    ) -> None:
        self.a2a = client
        self.roots = roots
        self.context_prefix = context_prefix
        self._reload_credentials = reload_credentials
        self._conn: Any = None
        self._sessions: dict[str, Session] = {}
        self._roots_loaded = False
        self._authed = False

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def initialize(self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None, **_: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=True),
            ),
            auth_methods=AUTH_METHODS,
            agent_info=Implementation(name="protoagent-acp", title="protoAgent", version=__version__),
        )

    async def authenticate(self, method_id: str, **_: Any) -> AuthenticateResponse:
        if self._reload_credentials is not None:
            creds = self._reload_credentials()
            self.a2a.token = creds.token
        self._authed = False
        await self._ensure_auth()
        return AuthenticateResponse()

    async def _ensure_auth(self) -> None:
        if self._authed:
            return
        try:
            await self.a2a.probe()
        except A2AUnauthorized as exc:
            raise RequestError.auth_required({"reason": str(exc)}) from exc
        except A2AError as exc:
            raise RequestError.internal_error({"reason": str(exc)}) from exc
        self._authed = True

    async def new_session(self, cwd: str, mcp_servers: Any = None, **_: Any) -> NewSessionResponse:
        await self._ensure_auth()
        if not self._roots_loaded:
            await self.roots.load(self.a2a)
            self._roots_loaded = True
            log.info("project roots (%s): %s", self.roots.source, self.roots.roots)
        # mcp_servers from the client are not forwarded: the instance's tool surface is
        # its own config (ADR 0111 — an MCP server Zed runs locally is not reachable from
        # a remote instance, and mounting per-session servers is not an A2A concept).
        sid = _new_context_id(self.context_prefix)
        self._sessions[sid] = Session(id=sid, cwd=cwd)
        return NewSessionResponse(session_id=sid)

    async def cancel(self, session_id: str, **_: Any) -> None:
        s = self._sessions.get(session_id)
        if s is None:
            return
        s.cancelled = True
        if s.task_id:
            with contextlib.suppress(A2AError):
                await self.a2a.cancel(s.task_id)
        if s.runner is not None and not s.runner.done():
            s.runner.cancel()

    # ── the turn ─────────────────────────────────────────────────────────────

    async def prompt(self, prompt: list[Any], session_id: str, **_: Any) -> PromptResponse:
        s = self._sessions.get(session_id)
        if s is None:
            raise RequestError.invalid_params({"sessionId": f"unknown session {session_id!r}"})
        text = prompt_text(prompt)
        task_id: str | None = None
        metadata: dict | None = None
        if s.parked_task_id:
            task_id, metadata = s.parked_task_id, {"hitl_resume": True}
            s.parked_task_id = None
        elif s.first_prompt:
            text = self._preamble(s) + text
        s.first_prompt = False
        s.cancelled = False
        s.spoke = False
        s.runner = asyncio.create_task(self._drive(s, text, task_id, metadata))
        try:
            stop, usage = await s.runner
        except asyncio.CancelledError:
            if s.cancelled:
                return PromptResponse(stop_reason="cancelled")
            raise
        finally:
            s.runner = None
            s.task_id = None
        return PromptResponse(stop_reason="cancelled" if s.cancelled else stop, usage=usage)

    def _preamble(self, s: Session) -> str:
        """Tell the agent where the operator is, once per session — only when the editor's
        folder IS one of its projects (a name it can pass to its fs tools)."""
        hit = self.roots.project_for(s.cwd.rstrip("/")) if s.cwd else None
        if not hit:
            return ""
        name, root = hit
        return (
            f"[Context: the operator is talking to you from the Zed editor, opened on your "
            f"project `{name}` ({root}). Paths you read or search there are shown to them in the editor.]\n\n"
        )

    async def _drive(self, s: Session, text: str, task_id: str | None, metadata: dict | None) -> tuple[str, Usage | None]:
        usage: Usage | None = None
        while True:
            streamed = ""
            paused: StateEvent | None = None
            last: StateEvent | None = None
            try:
                async for frame in self.a2a.stream(text, context_id=s.id, task_id=task_id, metadata=metadata):
                    cid = frame_context_id(frame)
                    if cid and cid != s.id:
                        continue  # cross-talk from another context: never ours
                    for evt in decode_frame(frame):
                        if isinstance(evt, TextEvent):
                            streamed = await self._on_text(s, evt, streamed)
                        elif isinstance(evt, ReasoningEvent):
                            await self._send(s, update_agent_thought_text(evt.text))
                        elif isinstance(evt, ToolEvent):
                            await self._on_tool(s, evt)
                        elif isinstance(evt, UsageEvent):
                            usage = Usage(
                                input_tokens=evt.input_tokens,
                                output_tokens=evt.output_tokens,
                                total_tokens=evt.input_tokens + evt.output_tokens,
                                cached_read_tokens=evt.cache_read_tokens or None,
                            )
                        elif isinstance(evt, StateEvent):
                            if evt.task_id:
                                s.task_id = evt.task_id
                            if evt.state:
                                last = evt
                            if evt.paused:
                                paused = evt
            except A2AUnauthorized as exc:
                raise RequestError.auth_required({"reason": str(exc)}) from exc
            except A2AError as exc:
                # The stream broke (or the server answered a JSON-RPC error). If a task
                # exists, its durable record says how the turn actually ended.
                if s.cancelled:
                    return "cancelled", usage
                if not s.task_id:
                    await self._fail(s, str(exc), state="unreachable")
                last, streamed = await self._recover(s, streamed, broke=str(exc))
                paused = last if last.paused else None
            else:
                if s.cancelled and (last is None or not last.terminal):
                    return "cancelled", usage
                if paused is None and (last is None or not last.terminal):
                    # The stream closed without a terminal frame. Never report that as a
                    # normal end_turn: ask the durable task how the turn ended.
                    last, streamed = await self._recover(s, streamed)
                    paused = last if last.paused else None

            if paused is None:
                if last.state in ("canceled", "cancelled"):
                    return "cancelled", usage
                if last.state in ("failed", "rejected"):
                    await self._fail(s, last.text or "the turn failed with no reason given", state=last.state)
                return "end_turn", usage

            hitl = paused.hitl or {}
            if hitl.get("kind") == "approval" and paused.task_id:
                decision = await self._ask_permission(s, paused.task_id, hitl)
                task_id, metadata, text = paused.task_id, {"hitl_resume": True}, decision
                continue  # resume the same task in the same prompt
            # A question / form: show it, park, and let the next prompt answer it.
            question = str(hitl.get("question") or hitl.get("title") or paused.text or "The agent needs input.")
            if hitl.get("kind") == "form":
                question += "\n\n(This is a form in the protoAgent console; reply here in words, or answer it there.)"
            sep = "\n\n" if s.spoke else ""
            await self._send(s, update_agent_message_text(f"{sep}**Input needed:** {question}"))
            s.parked_task_id = paused.task_id or None
            return "end_turn", usage

    async def _recover(self, s: Session, streamed: str, broke: str = "") -> tuple[StateEvent, str]:
        """Ask ``GetTask`` how the turn ended; emit any answer text the stream never
        delivered. Raises (via :meth:`_fail`) when the outcome can't be established or the
        task is still running — a silent ``end_turn`` would read as success in the editor."""
        task = await self.a2a.get_task(s.task_id) if s.task_id else None
        state, text = task_snapshot(task)
        if state is None or not state.state:
            why = f"the connection broke ({broke})" if broke else "the stream closed without a result"
            await self._fail(s, f"{why}, and the task could not be read back", state="unknown")
        if state.terminal and state.state == "completed" and text:
            streamed = await self._on_text(s, TextEvent(text=text, append=False), streamed)
        if not state.terminal and not state.paused:
            why = f"the connection broke ({broke})" if broke else "the stream closed early"
            await self._fail(
                s,
                f"{why}; task {state.task_id} is still {state.state} and may finish on its own — "
                "check the protoAgent console",
                state=state.state,
            )
        return state, streamed

    async def _fail(self, s: Session, raw: str, *, state: str) -> NoReturn:
        """Surface a failed turn in BOTH places Zed shows it: a message chunk (kept in the
        thread's history) and a JSON-RPC error on ``session/prompt`` (Zed renders it as an
        error callout; a plain end_turn would look like an empty success)."""
        message = friendly_error(raw)
        sep = "\n\n" if s.spoke else ""
        await self._send(s, update_agent_message_text(f"{sep}⚠️ protoAgent error: {message}"))
        raise RequestError(-32603, f"protoAgent: {message}", {"state": state, "taskId": s.task_id, "detail": raw[:2000]})

    async def _on_text(self, s: Session, evt: TextEvent, streamed: str) -> str:
        if evt.append:
            chunk = evt.text if streamed.strip() else evt.text.lstrip()
            if chunk:
                await self._send(s, update_agent_message_text(chunk))
            return streamed + evt.text
        # A REPLACE: the first chunk of the answer, or the terminal authoritative text.
        # ACP chunks can't be retracted, so emit only what the client hasn't seen.
        if not streamed:
            first = evt.text.lstrip()  # the producer's paragraph break before a turn's first words
            if first:
                await self._send(s, update_agent_message_text(first))
            return evt.text
        if evt.text.startswith(streamed):
            tail = evt.text[len(streamed) :]
            if tail:
                await self._send(s, update_agent_message_text(tail))
            return evt.text
        if "".join(evt.text.split()) != "".join(streamed.split()):
            log.warning("terminal text diverged from the streamed text; keeping what was streamed")
        return streamed

    async def _on_tool(self, s: Session, evt: ToolEvent) -> None:
        if not evt.id:
            return
        if evt.phase == "start":
            args = toolmap.parse_args(evt.args)
            if args:
                s.args[evt.id] = args
            args = s.args.get(evt.id, {})
            if args.get("project"):
                await self.roots.refresh_if_unknown(self.a2a, args.get("project"))
            title, locs = toolmap.describe(evt.name, args, self.roots)
            if evt.parent_id:
                title = f"↳ {title}"
            locations = [ToolCallLocation(**loc) for loc in locs] or None
            if evt.id not in s.announced:
                s.announced.add(evt.id)
                await self._send(
                    s,
                    start_tool_call(
                        evt.id,
                        title,
                        kind=toolmap.tool_kind(evt.name),
                        status="in_progress",
                        locations=locations,
                        raw_input=args or None,
                    ),
                )
            else:
                # The second announcement carries the real args: fill the card in.
                await self._send(s, update_tool_call(evt.id, title=title, locations=locations, raw_input=args or None))
            return
        # end
        if evt.id not in s.announced:  # missed start: still show it
            s.announced.add(evt.id)
            title, _ = toolmap.describe(evt.name, {}, self.roots)
            await self._send(s, start_tool_call(evt.id, title, kind=toolmap.tool_kind(evt.name), status="in_progress"))
        args = s.args.pop(evt.id, {})
        if evt.name == "list_projects":
            self.roots.learn_from_list_projects(evt.result)
        extra = toolmap.result_locations(evt.name, args, evt.result, self.roots)
        body = toolmap.result_text(evt.result)
        await self._send(
            s,
            update_tool_call(
                evt.id,
                status="failed" if evt.error else "completed",
                content=[tool_content(text_block(body))] if body else None,
                locations=[ToolCallLocation(**loc) for loc in extra] or None,
                raw_output=evt.result,
            ),
        )

    async def _ask_permission(self, s: Session, task_id: str, hitl: dict) -> str:
        title = str(hitl.get("title") or "Approve this action?")
        detail = hitl.get("detail") or hitl.get("command") or ""
        tool_call_id = f"approval-{task_id}-{uuid.uuid4().hex[:6]}"
        await self._send(
            s,
            start_tool_call(tool_call_id, f"{title} {detail}".strip(), kind="execute" if hitl.get("command") else "other", status="pending"),
        )
        try:
            resp = await self._conn.request_permission(
                session_id=s.id,
                tool_call=ToolCallUpdate(tool_call_id=tool_call_id, title=title),
                options=[
                    PermissionOption(option_id="approve", name="Approve", kind="allow_once"),
                    PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
                ],
            )
            outcome = resp.outcome
            approved = getattr(outcome, "outcome", "") == "selected" and getattr(outcome, "option_id", "") == "approve"
        except Exception as exc:  # a client without permission UI: fail closed
            log.warning("request_permission failed (%s); denying", exc)
            approved = False
        await self._send(s, update_tool_call(tool_call_id, status="completed" if approved else "failed"))
        return "approved" if approved else "denied"

    async def _send(self, s: Session, update: Any) -> None:
        if getattr(update, "session_update", None) == "agent_message_chunk":
            s.spoke = True
        if self._conn is not None:
            await self._conn.session_update(session_id=s.id, update=update)
