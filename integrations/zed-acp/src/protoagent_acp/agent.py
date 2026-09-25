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
    update_user_message_text,
)
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AuthEnvVar,
    EnvVarAuthMethod,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionResumeCapabilities,
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
    SteerConsumedEvent,
    TextEvent,
    ToolEvent,
    UsageEvent,
    decode_frame,
    frame_context_id,
    task_snapshot,
)
from . import history
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
    # Steer-by-"Send Now" (ADR 0111): Zed's Send Now is session/cancel → session/prompt. A
    # cancel DETACHES the running turn for a grace window instead of killing it — the old
    # prompt answers `cancelled` at once, the stream keeps draining into `held` — and a
    # prompt arriving inside the window is queued into the running turn as a steer and
    # ADOPTS the stream. No prompt in time → the real CancelTask.
    owner: asyncio.Future | None = None  # the prompt currently receiving this turn's result
    detached: bool = False
    held: list = field(default_factory=list)  # updates produced while detached
    attached: asyncio.Event = field(default_factory=asyncio.Event)
    grace_timer: asyncio.Task | None = None
    steer: tuple[str, str] | None = None  # (id, text) queued by a Send Now, not yet folded in
    handoff: asyncio.Task | None = None  # a replay/notice sent after the handler's response (the barrier)
    # Durable-history bookkeeping, so a replay never repeats a turn and never shows a
    # half-written answer as final: task ids already shown in full (replayed, or streamed
    # live here), and ones shown only as "(still running in the console…)".
    shown: set[str] = field(default_factory=set)
    shown_running: set[str] = field(default_factory=set)  # also parked turns: they continue later
    shown_tools: set[str] = field(default_factory=set)
    shown_text: dict[str, str] = field(default_factory=dict)  # tid → answer text already shown
    parked_hitl: dict | None = None  # the pending question/form a loaded chat is parked on
    spoke: bool = False  # any answer text sent this prompt (for separators)
    # "Allow for this session" (allow_always on a run_command approval). In memory only —
    # never persisted; a new Zed thread starts prompting again.
    allow_commands: bool = False  # send bypass_permissions on every later A2A message
    auto_approve_turn: bool = False  # the server's bypass starts NEXT message: cover this turn
    told_bypass_refused: bool = False
    open_tools: dict[str, str] = field(default_factory=dict)  # started, not yet ended: id -> name
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


def _is_busy(summary: dict | None) -> bool:
    return bool(summary) and summary.get("active") is True


def _still_parked(summary: dict | None) -> bool:
    """Is the session still parked on its question? Without a ``last_state`` the server
    can't say — trust what we saw at load time."""
    if not summary or not summary.get("last_state"):
        return True
    return "INPUT_REQUIRED" in str(summary["last_state"]).upper()


def continuing_notice(title: str) -> str:
    """↪ Continuing your console chat “<title>”. — one terminal mark, never “…briefly.”."""
    title = " ".join((title or "").split())
    if not title:
        return "\u21aa Continuing your console chat."
    end = "" if title[-1] in ".!?\u2026" else "."
    return f"\u21aa Continuing your console chat \u201c{title}\u201d{end}"


def _text_of_status(status: Any) -> str:
    msg = status.get("message") if isinstance(status, dict) else None
    parts = msg.get("parts") if isinstance(msg, dict) else None
    return "".join(str(p.get("text")) for p in parts or [] if isinstance(p, dict) and p.get("text"))


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
        steer_grace: float = 1.5,
        thread_index: history.ThreadIndex | None = None,
        zed_threads_only: bool = False,
        busy_poll: float = 2.0,
        busy_timeout: float = 120.0,
    ) -> None:
        self.zed_threads_only = zed_threads_only
        self.busy_poll = busy_poll
        self.busy_timeout = busy_timeout
        self.steer_grace = steer_grace
        self.index = thread_index or history.ThreadIndex(client.base_url)
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
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=True),
                session_capabilities=SessionCapabilities(list=SessionListCapabilities(), resume=SessionResumeCapabilities()),
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
        claimed = await self._claim_handoff(cwd)
        if claimed is not None:
            return NewSessionResponse(session_id=claimed)
        sid = _new_context_id(self.context_prefix)
        self._sessions[sid] = Session(id=sid, cwd=cwd)
        self.index.put(sid, cwd=cwd)
        return NewSessionResponse(session_id=sid)

    async def _claim_handoff(self, cwd: str) -> str | None:
        """Console → Zed hand-off: the operator pressed "Continue in Zed" (or the agent ran
        ``open_in_editor``) and then started a thread here. ``POST /api/editor/handoff/claim
        {cwd}`` → 200 ``{session_id, project, path, line, title}`` adopts that chat —
        same contextId, history replayed like session/load; 204 / 404 / anything else → a
        fresh thread. One claim per session/new; the server deletes it on claim."""
        status, body = await self.a2a.send_json("POST", "/api/editor/handoff/claim", {"cwd": cwd})
        if status != 200 or not isinstance(body, dict) or not body.get("session_id"):
            return None
        sid = str(body["session_id"])
        try:
            s, turns = await self._register(sid, cwd)
        except RequestError as exc:
            log.warning("hand-off to %s could not be loaded (%s) — starting a fresh thread", sid, exc)
            return None
        # The replay must follow the session/new RESPONSE (the client learns the session id
        # from it); scheduled on the loop, it runs right after this handler returns.
        title = str(body.get("title") or "") or next(
            (history.title_of(history.first_user_text(t)) for t in turns if history.first_user_text(t)), ""
        )
        if title:
            self.index.put(sid, title=title)
        s.handoff = asyncio.create_task(self._after_response(s, self._replay_handoff(s, turns, title, body)))
        return sid

    async def _replay_handoff(self, s: Session, turns: list[dict], title: str, claim: dict) -> None:
        await self._replay(s, turns)
        where = self.roots.resolve(claim.get("project"), claim.get("path")) if claim.get("path") else None
        if where:  # the file the console was looking at: a location follow-the-agent can jump to
            loc: dict[str, Any] = {"path": where}
            line = claim.get("line")
            if isinstance(line, int) and line >= 1:
                loc["line"] = line
            await self._send(
                s,
                start_tool_call(f"handoff-{uuid.uuid4().hex[:6]}", f"Open {claim.get('path')}", kind="read",
                                status="completed", locations=[ToolCallLocation(**loc)]),
            )
        await self._send(s, update_agent_message_text("\n\n" + continuing_notice(title)))
        await self._announce_pending(s)

    # ── thread history (session/list, session/load, session/resume) ─────────────

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **_: Any) -> ListSessionsResponse:
        await self._ensure_auth()
        rows = await history.list_threads(self.a2a, self.index, self.context_prefix if self.zed_threads_only else None, cwd)
        return ListSessionsResponse(
            sessions=[SessionInfo(session_id=r["sessionId"], cwd=r["cwd"], title=r["title"], updated_at=r["updatedAt"]) for r in rows]
        )

    async def _register(self, session_id: str, cwd: str) -> tuple[Session, list[dict]]:
        await self._ensure_auth()
        # Ids are opaque: any session the server knows is loadable (a console chat too); the
        # /turns read below is the existence check. --zed-threads-only keeps the old fence.
        if self.zed_threads_only and not session_id.startswith(self.context_prefix + "-"):
            raise RequestError.invalid_params({"sessionId": f"not a {self.context_prefix} thread: {session_id!r}"})
        if not self._roots_loaded:
            await self.roots.load(self.a2a)
            self._roots_loaded = True
        turns = await history.fetch_turns(self.a2a, session_id)
        if not turns:
            raise RequestError.resource_not_found(session_id)
        s = self._sessions.get(session_id) or Session(id=session_id, cwd=cwd)
        s.cwd, s.first_prompt = cwd, False  # the server already has this thread's context
        # A parked turn is NOT answered implicitly: _replay records it and the caller shows
        # the pending question + an explicit notice before the next prompt may resume it.
        self._sessions[session_id] = s
        self.index.put(session_id, cwd=cwd)
        return s, turns

    async def resume_session(self, cwd: str, session_id: str, **_: Any) -> ResumeSessionResponse:
        """Re-attach without replaying history — but a chat parked on a question or form still
        gets its question and the explicit notice (after the response: the replay barrier),
        or the next prompt would be swallowed as the answer unannounced."""
        s, turns = await self._register(session_id, cwd)
        for turn in turns:
            if turn.get("task_id"):
                s.shown.add(str(turn["task_id"]))  # the client already has these
        self._note_pending(s, turns)
        if s.parked_hitl is not None:
            s.handoff = asyncio.create_task(self._after_response(s, self._announce_pending(s, show_question=True)))
        return ResumeSessionResponse()

    async def load_session(self, cwd: str, session_id: str, **_: Any) -> LoadSessionResponse:
        """Replay the durable thread as session/update notifications — the operator's
        messages, each turn's tool calls (completed, with locations) and its answer — then
        keep the contextId, so the next prompt continues with the agent's memory intact."""
        s, turns = await self._register(session_id, cwd)
        await self._replay(s, turns)
        await self._announce_pending(s)
        return LoadSessionResponse()

    def _note_pending(self, s: Session, turns: list[dict]) -> None:
        """Record the chat's pending question/form — only the LAST turn counts (an older
        parked turn the operator moved on from is history, not a pending question)."""
        s.parked_task_id, s.parked_hitl = None, None
        last = turns[-1] if turns else None
        if last is not None and history.turn_state(last) == "input-required" and last.get("task_id"):
            hitl = history.pending_hitl(last) or {}
            s.parked_hitl = hitl
            # An approval is a decision, not text: it's answered in the console (a stray
            # message here must never read as "approved"). Questions and forms can be
            # answered from here, explicitly.
            if history.hitl_kind(hitl) != "approval":
                s.parked_task_id = str(last["task_id"])

    async def _replay(self, s: Session, turns: list[dict]) -> None:
        """Replay durable turns not yet shown in this thread: the operator's message, steers
        and tool calls (completed, with locations) and the answer. A turn still RUNNING is
        shown as "(still running in the console…)" instead of its half-written text, and
        finished on a later replay. The pending question of a parked last turn is rendered."""
        self._note_pending(s, turns)
        for i, turn in enumerate(turns):
            tid = str(turn.get("task_id") or "")
            if tid and tid in s.shown:
                continue
            state = history.turn_state(turn)
            header_shown = tid in s.shown_running
            if not header_shown:
                user = history.clean_user_text(history.first_user_text(turn))
                if user:
                    await self._send(s, update_user_message_text(user))
            if state in history.RUNNING_STATES:
                if not header_shown:
                    await self._send(s, update_agent_message_text("(still running in the console…)"))
                    s.shown_running.add(tid)
                continue
            for kind, item in history.turn_events(turn):
                if kind == "steer":  # a Send Now redirect, where the agent read it
                    if not header_shown:
                        await self._send(s, update_user_message_text(item))
                    continue
                call = item
                if call["id"] in s.shown_tools:
                    continue
                s.shown_tools.add(call["id"])
                args = toolmap.parse_args(call.get("args"))
                title, locs = toolmap.describe(call["name"], args, self.roots)
                locs += toolmap.result_locations(call["name"], args, call.get("result"), self.roots)
                body = toolmap.result_text(call.get("result"))
                await self._send(
                    s,
                    start_tool_call(
                        f"replay-{call['id']}",
                        title,
                        kind=toolmap.tool_kind(call["name"]),
                        status="failed" if call.get("error") else "completed",
                        locations=[ToolCallLocation(**loc) for loc in locs] or None,
                        content=[tool_content(text_block(body))] if body else None,
                        raw_input=args or None,
                    ),
                )
            answer = str(turn.get("text") or "").strip()
            seen = s.shown_text.get(tid, "")
            fresh = answer[len(seen):].strip() if seen and answer.startswith(seen) else ("" if answer == seen else answer)
            if fresh:
                await self._send(s, update_agent_message_text(fresh))
            elif state == "failed" and not seen:
                reason = _text_of_status(turn.get("status"))
                await self._send(s, update_agent_message_text(f"⚠️ protoAgent error: {friendly_error(reason or 'the turn failed')}"))
            if state == "input-required":
                if i == len(turns) - 1:
                    await self._send(s, update_agent_message_text("\n\n" + history.render_hitl(s.parked_hitl or {})))
                if tid:  # parked: it may continue (answered here or in the console) — not final
                    s.shown_running.add(tid)
                    s.shown_text[tid] = answer
                continue
            if tid:
                s.shown.add(tid)
                s.shown_running.discard(tid)
                s.shown_text.pop(tid, None)

    async def _announce_pending(self, s: Session, *, show_question: bool = False) -> None:
        """The explicit notice that makes the NEXT prompt the parked question's answer."""
        hitl = s.parked_hitl
        if hitl is None:
            return
        if show_question:
            await self._send(s, update_agent_message_text(history.render_hitl(hitl) + "\n\n"))
        what = history.hitl_prompt(hitl).rstrip(" :")
        what += "" if what[-1:] in (".", "?", "!", "\u2026") else "."
        kind = history.hitl_kind(hitl)
        if kind == "approval":
            text = (f"This chat is waiting on an approval in the console: {what} Approve or deny it there — "
                    "a message here won't answer it.")
        else:
            noun = "a form" if kind == "form" else "a question"
            text = (f"This chat is waiting on {noun} from the console: {what} Your next message here will be "
                    "sent as the answer — or answer it in the console.")
        await self._send(s, update_agent_message_text(("\n\n" if s.spoke else "") + text))

    async def _after_response(self, s: Session, coro: Any) -> None:
        """The replay barrier: run ``coro`` only once the current handler's response is on
        the wire. The SDK writes through one FIFO queue and queues the response the moment
        the handler returns; a short beat means our session/update notifications can never
        overtake it (a client drops updates for a session it hasn't been told about)."""
        await asyncio.sleep(0.05)
        await coro

    async def cancel(self, session_id: str, **_: Any) -> None:
        s = self._sessions.get(session_id)
        if s is None:
            return
        live = s.runner is not None and not s.runner.done()
        if live and self.steer_grace > 0 and not s.detached:
            # Maybe a Send Now: answer the prompt `cancelled` now (Zed waits for that before
            # sending the queued message), keep the turn running unseen for the grace window.
            s.detached = True
            s.attached.clear()
            self._resolve_owner(s, ("cancelled", None))
            s.grace_timer = asyncio.create_task(self._grace_expired(s))
            return
        await self._hard_cancel(s)

    async def _grace_expired(self, s: Session) -> None:
        await asyncio.sleep(self.steer_grace)
        if s.detached:
            log.info("no prompt within %.1fs of the cancel — cancelling task %s", self.steer_grace, s.task_id)
            await self._hard_cancel(s)

    async def _hard_cancel(self, s: Session) -> None:
        s.cancelled = True
        if s.grace_timer is not None and s.grace_timer is not asyncio.current_task():
            s.grace_timer.cancel()
        s.grace_timer = None
        s.detached = False
        s.held.clear()
        s.attached.set()  # release anything waiting to ask for permission; it will be cancelled
        if s.task_id:
            with contextlib.suppress(A2AError):
                await self.a2a.cancel(s.task_id)
        if s.runner is not None and not s.runner.done():
            s.runner.cancel()
            with contextlib.suppress(BaseException):
                await s.runner
        self._resolve_owner(s, ("cancelled", None))

    @staticmethod
    def _resolve_owner(s: Session, result: tuple[str, Usage | None]) -> None:
        if s.owner is not None and not s.owner.done():
            s.owner.set_result(result)

    # ── the turn ─────────────────────────────────────────────────────────────

    async def prompt(self, prompt: list[Any], session_id: str, **_: Any) -> PromptResponse:
        s = self._sessions.get(session_id)
        if s is None:
            raise RequestError.invalid_params({"sessionId": f"unknown session {session_id!r}"})
        text = prompt_text(prompt)
        if s.detached:
            if s.runner is not None and not s.runner.done():
                steered = await self._adopt_as_steer(s, text)
                if steered is not None:
                    return steered
            else:  # the turn finished inside the window: nothing to steer, a normal new turn
                s.detached = False
                s.held.clear()
                if s.grace_timer is not None:
                    s.grace_timer.cancel()
        return await self._start_turn(s, text)

    async def _start_turn(self, s: Session, text: str) -> PromptResponse:
        # Every path — a fresh turn AND a resume — waits for any pending replay (the
        # barrier) and for a console turn to finish (the busy check).
        if s.handoff is not None:
            with contextlib.suppress(Exception):
                await s.handoff
            s.handoff = None
        s.cancelled = False
        s.spoke = False
        summary, waited = await self._wait_until_free(s)
        if s.cancelled:
            return PromptResponse(stop_reason="cancelled")
        if waited or (s.parked_task_id and not _still_parked(summary)):
            # The console moved the chat on while we waited (or answered the form there):
            # show what happened before sending, so the operator isn't answering blind.
            before = s.parked_task_id
            turns = await history.fetch_turns(self.a2a, s.id)
            if turns:
                await self._replay(s, turns)
            if s.parked_task_id and s.parked_task_id != before:
                # The console turn ended on a NEW question/form — which this message was not
                # written to answer. Announce it; don't consume the message as the answer.
                await self._announce_pending(s)
                await self._send(s, update_agent_message_text(
                    "\n\nYour message wasn't sent. Send it again to answer, or answer in the console."))
                return PromptResponse(stop_reason="end_turn")
        task_id: str | None = None
        metadata: dict | None = None
        if s.parked_task_id:  # announced explicitly (live park, load/claim/resume notice): once
            task_id, metadata = s.parked_task_id, {"hitl_resume": True}
            s.parked_task_id, s.parked_hitl = None, None
        else:
            s.open_tools.clear()  # a fresh turn: nothing from an earlier one is still pending
            if s.first_prompt:
                self.index.put(s.id, title=history.title_of(text))
                text = self._preamble(s) + text
        s.first_prompt = False
        s.cancelled = False
        s.spoke = False
        s.auto_approve_turn = False
        s.attached.set()
        runner = asyncio.create_task(self._drive(s, text, task_id, metadata))
        s.runner = runner
        runner.add_done_callback(lambda t, s=s: self._turn_finished(s, t))
        return await self._own(s, runner)

    def _turn_finished(self, s: Session, task: asyncio.Task) -> None:
        if s.runner is task:
            s.runner = None
            s.task_id = None
        if s.detached and s.steer is None:
            # Finished while nobody was attached and no Send Now is adopting it: dropped.
            s.held.clear()

    async def _summary(self, s: Session) -> dict | None:
        """``GET /api/chat/sessions/<id>`` → ``{active, last_state, …}``, or ``None`` when the
        server can't say (older server, unknown session, an error)."""
        status, body = await self.a2a.send_json("GET", f"/api/chat/sessions/{s.id}")
        return body if status == 200 and isinstance(body, dict) else None

    async def _wait_until_free(self, s: Session) -> tuple[dict | None, bool]:
        """Never interleave with a console turn: wait while the session is busy. Returns
        ``(latest summary, waited)``; sets ``s.cancelled`` if the operator cancelled while
        waiting; a RequestError after ``busy_timeout``. No ``active`` field → no wait."""
        summary = await self._summary(s)
        if not _is_busy(summary):
            return summary, False
        # The server brackets a turn as active until its stream fully unwinds, so right
        # after OUR previous turn (a Send Now re-run, a quick follow-up) it can still read
        # busy for a moment. Re-check once, silently, before telling the operator.
        await asyncio.sleep(0.3)
        summary = await self._summary(s)
        if not _is_busy(summary):
            return summary, False
        await self._send(s, update_agent_message_text("This chat is busy in the console. I'll send when it's free.\n\n"))
        deadline = time.monotonic() + self.busy_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(min(self.busy_poll, 2.0))
            if s.cancelled:
                return summary, True
            summary = await self._summary(s)
            if not _is_busy(summary):
                return summary, True
        raise RequestError(
            -32603,
            f"protoAgent: this chat stayed busy in the console for {int(self.busy_timeout)}s — "
            "your message was not sent; try again when that turn finishes",
            {"state": "busy", "sessionId": s.id},
        )

    async def _own(self, s: Session, runner: asyncio.Task) -> PromptResponse:
        """Wait for ``runner``'s result — or for a detach (session/cancel) to answer this
        prompt `cancelled` while the turn keeps going. The runner is passed in, not read
        from the session: an adopted turn can finish (clearing ``s.runner``) while the
        adopting prompt is still flushing held output."""
        owner: asyncio.Future = asyncio.get_running_loop().create_future()
        s.owner = owner
        await asyncio.wait({runner, owner}, return_when=asyncio.FIRST_COMPLETED)
        if owner.done():
            stop, usage = owner.result()
            return PromptResponse(stop_reason=stop, usage=usage)
        s.owner = None
        try:
            stop, usage = runner.result()
        except asyncio.CancelledError:
            return PromptResponse(stop_reason="cancelled")
        return PromptResponse(stop_reason="cancelled" if s.cancelled else stop, usage=usage)

    async def _adopt_as_steer(self, s: Session, text: str) -> PromptResponse | None:
        """A prompt inside the grace window: queue it into the running turn
        (``POST /api/chat/sessions/<contextId>/steer`` — the console's mid-turn steering,
        folded in at the next model call) and make THIS prompt the stream's owner. ``None``
        when the steer can't be queued: the caller cancels for real and starts a new turn."""
        runner = s.runner
        assert runner is not None
        steer_id = f"zed-{uuid.uuid4().hex[:12]}"
        s.steer = (steer_id, text)  # set BEFORE the POST: the fold frame can race the reply
        status, body = await self.a2a.send_json("POST", f"/api/chat/sessions/{s.id}/steer", {"id": steer_id, "text": text})
        if status != 200 or not (isinstance(body, dict) and body.get("ok")):
            log.warning("steer POST failed (%s) — cancelling and starting a new turn", status)
            s.steer = None
            await self._hard_cancel(s)
            return None
        if s.grace_timer is not None:
            s.grace_timer.cancel()
            s.grace_timer = None
        s.detached = False
        s.cancelled = False
        s.spoke = False
        held, s.held = s.held, []
        for update in held:  # in stream order; includes the marker if the fold already happened
            await self._send(s, update)
        s.attached.set()
        resp = await self._own(s, runner)
        if resp.stop_reason != "end_turn":
            return resp
        # Turn over. A steer that arrived after the turn's LAST model call was never folded
        # in; take it back out of the queue and run it as the next turn (the console does
        # the same reconciliation at turn end).
        _, queued = await self.a2a.send_json("GET", f"/api/chat/sessions/{s.id}/steer")
        pending = [i.get("id") for i in (queued or {}).get("pending") or [] if isinstance(i, dict)]
        if steer_id in pending:
            _, gone = await self.a2a.send_json("DELETE", f"/api/chat/sessions/{s.id}/steer/{steer_id}")
            if isinstance(gone, dict) and gone.get("removed"):
                s.steer = None
                await self._steer_marker(s, text)
                return await self._start_turn(s, text)
        if s.steer is not None:
            # Folded in, but this server doesn't emit the boundary frame: mark it at the end.
            s.steer = None
            await self._steer_marker(s, text)
        return resp

    async def _steer_marker(self, s: Session, text: str) -> None:
        short = " ".join(text.split())
        await self._send(s, update_agent_message_text(f"\n\n↪ steering: {short[:60]}{'…' if len(short) > 60 else ''}\n\n"))

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
                async for frame in self.a2a.stream(text, context_id=s.id, task_id=task_id, metadata=self._metadata(s, metadata)):
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
                        elif isinstance(evt, SteerConsumedEvent):
                            if s.steer is not None and s.steer[0] in evt.ids:
                                # The exact point the agent read the operator's redirect.
                                await self._steer_marker(s, s.steer[1])
                                s.steer = None
                        elif isinstance(evt, StateEvent):
                            if evt.task_id:
                                s.task_id = evt.task_id
                                s.shown.add(evt.task_id)  # shown live: never replay it
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
            s.parked_hitl = hitl
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
            s.open_tools[evt.id] = evt.name
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
        s.open_tools.pop(evt.id, None)
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

    @staticmethod
    def _metadata(s: Session, extra: dict | None) -> dict | None:
        """A2A message metadata for this session: the caller's (e.g. ``hitl_resume``) plus
        ``bypass_permissions: true`` once the operator chose "Allow for this session" — the
        same key, in the same place (``message.metadata``), the console's /bypass sends
        (``apps/web/src/lib/api.ts``); ``tools/fs_tools.py::_bypass_requested`` reads it."""
        md = dict(extra or {})
        if s.allow_commands:
            md["bypass_permissions"] = True
        return md or None

    def _pending_tool(self, s: Session, hitl: dict) -> str:
        """Which tool parked for approval: the most recent started-but-unfinished call
        (the park happens inside it, so it has no tool_end yet). Falls back to the
        approval's title for a stream that never announced the call."""
        if s.open_tools:
            return next(reversed(s.open_tools.values()))
        title = str(hitl.get("title") or "").lower()
        if "shell command" in title:
            return "run_command"
        if "delete" in title:
            return "delete_file"
        return ""

    async def _ask_permission(self, s: Session, task_id: str, hitl: dict) -> str:
        if s.detached:
            # Parked for approval inside a Send Now window: ask whoever adopts the turn (a
            # hard cancel sets the event too, and cancels this runner).
            await s.attached.wait()
        title = str(hitl.get("title") or "Approve this action?")
        detail = str(hitl.get("detail") or hitl.get("command") or "")
        tool = self._pending_tool(s, hitl)
        # Only a shell-command approval can be allowed for the session: that is the one gate
        # the server's bypass skips. delete_file's permanent-delete floor ALWAYS asks (ADR
        # 0083 D5) and anything else (a plugin's approval) has no server-side bypass, so
        # neither is ever offered allow_always nor auto-approved here.
        session_allowable = tool == "run_command"
        tool_call_id = f"approval-{task_id}-{uuid.uuid4().hex[:6]}"
        kind = "execute" if tool == "run_command" else ("delete" if tool == "delete_file" else "other")
        first_line = detail.splitlines()[0] if detail else ""

        if session_allowable and s.auto_approve_turn:
            await self._send(
                s,
                start_tool_call(tool_call_id, f"Allowed for this session: {first_line}".strip(), kind=kind, status="completed"),
            )
            return "approved"
        if session_allowable and s.allow_commands and not s.told_bypass_refused:
            # We sent bypass_permissions and the server STILL parked: this instance forbids
            # bypass (filesystem.bypass_allowed: false). Respect it — ask, don't work around.
            s.told_bypass_refused = True
            await self._send(
                s,
                update_agent_message_text(
                    ("\n\n" if s.spoke else "")
                    + "(This instance doesn't allow skipping command approval, so you'll be asked each time.)\n\n"
                ),
            )

        await self._send(s, start_tool_call(tool_call_id, f"{title} {first_line}".strip(), kind=kind, status="pending"))
        options = [PermissionOption(option_id="approve", name="Allow once", kind="allow_once")]
        if session_allowable:
            options.append(PermissionOption(option_id="approve_session", name="Allow for this session", kind="allow_always"))
        options.append(PermissionOption(option_id="deny", name="Deny", kind="reject_once"))
        choice = ""
        try:
            resp = await self._conn.request_permission(
                session_id=s.id,
                tool_call=ToolCallUpdate(tool_call_id=tool_call_id, title=title, raw_input={"detail": detail} if detail else None),
                options=options,
            )
            outcome = resp.outcome
            if getattr(outcome, "outcome", "") == "selected":  # AllowedOutcome; DeniedOutcome = dismissed
                choice = str(getattr(outcome, "option_id", ""))
        except Exception as exc:  # a client without permission UI: fail closed
            log.warning("request_permission failed (%s); denying", exc)
        approved = choice in ("approve", "approve_session")
        if choice == "approve_session" and session_allowable:
            if not s.allow_commands:
                s.allow_commands = True
                await self._send(
                    s,
                    update_agent_message_text(
                        ("\n\n" if s.spoke else "") + "Commands will run without asking for the rest of this thread.\n\n"
                    ),
                )
            s.auto_approve_turn = True
        await self._send(s, update_tool_call(tool_call_id, status="completed" if approved else "failed"))
        return "approved" if approved else "denied"

    async def _send(self, s: Session, update: Any) -> None:
        if s.detached:  # nobody owns the turn right now (the Send Now window): hold it
            s.held.append(update)
            return
        if getattr(update, "session_update", None) == "agent_message_chunk":
            s.spoke = True
        if self._conn is not None:
            await self._conn.session_update(session_id=s.id, update=update)
