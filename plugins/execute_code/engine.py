"""execute_code — a sandboxed Python code interpreter for the agent (bd-pe2.6).

The model writes a single Python script; it runs in an isolated child process
and its stdout comes back. This is **general-purpose code execution** — the
script can do anything Python can (compute, parse, transform data, call out).

Its headline use is **programmatic tool-calling**: instead of emitting one tool
call per turn (think → call → read → think …), the script calls several tools,
loops/filters/composes their results in code, and prints only what matters —
collapsing N round-trips into one turn (the model reads just the stdout, not
every intermediate payload). But the script is **not** limited to tool calls.

How it runs
-----------
The script executes in a **child Python process** (``python -u <tmpfile>`` — the
venv's own interpreter from source; the managed CPython runtime on the packaged
desktop app, ADR 0094) with:

- a **scrubbed environment** — only ``PATH`` (plus the bridge's loopback port +
  per-run token) is passed, so gateway keys / auth tokens in the parent env are
  never visible to the script;
- a **hard timeout** (the plugin's ``timeout`` setting) after which its whole
  process tree is killed (``infra.proc``, ADR 0098);
- a **tool-RPC bridge**: the script gets a ``tools`` object whose attributes are
  proxies for the exposed tools. Calling ``tools.web_search(query=...)``
  serialises the call over a loopback socket back to the **parent**, which runs
  the real (async) tool and returns the result. Tools therefore execute with the
  parent's credentials and audit/trace context — the child only orchestrates.
  Bridged calls are thread-safe and run concurrently (up to 8 per run). Use
  ``concurrent.futures.ThreadPoolExecutor`` for independent calls.

Security posture
----------------
Off in its own manifest, but ON by default through the bundled cowork pack (its
``enables: [execute_code]``, #3450); ``plugins.disabled: [execute_code]`` always turns it
off, and ``plugins.enabled`` turns it on without cowork. Runs **arbitrary model-authored
code**. Subprocess + env-scrub + timeout is *isolation, not a true sandbox*: the
child can still touch the filesystem and network as the server user. The ``tools``
allowlist only scopes the convenience bridge — it is **not** a security boundary;
the script can ``import os`` and do anything regardless. Enable only for
trusted-model output or inside a hardened container (seccomp / read-only FS /
network policy). The ``execute_code`` tool never exposes itself, so a script
can't recurse into more code execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.prebuilt import InjectedState

from infra.proc import akill_tree, group_kwargs, track_tree, untrack_tree, untrack_when_reaped

log = logging.getLogger(__name__)

# Prelude prepended to the user's script in the child process. Sets up the
# `tools` proxy object that bridges calls back to the parent over a loopback TCP
# socket whose port + per-run token are named in the environment. A socket
# (not anonymous pipes + fd inheritance) is what makes the bridge portable to
# Windows, where fd numbers don't survive spawn and anon pipes aren't
# Proactor-drivable (ADR 0098 / #2449). Kept dependency-free (stdlib only).
_RUNNER_PRELUDE = r'''
import os as _os, sys as _sys, json as _json, socket as _socket, threading as _threading

_sock = _socket.create_connection(("127.0.0.1", int(_os.environ["EC_PORT"])))
_REQ = _sock.makefile("w")   # child -> parent
_RESP = _sock.makefile("r")  # parent -> child
_REQ.write(_os.environ["EC_TOKEN"] + "\n")  # authenticate this run's connection
_REQ.flush()
_SEQ = 0
# Writes and waiter registration share a lock; only the reader thread reads replies.
_LOCK = _threading.Lock()
_PENDING = {}
_READER_STARTED = False
_CLOSED = False

def _ec_fail_waiters(_error):
    # Called under _LOCK.
    for _waiter in _PENDING.values():
        _waiter[1] = {"ok": False, "error": _error}
        _waiter[0].set()
    _PENDING.clear()

def _ec_read_replies():
    global _CLOSED
    try:
        while True:
            _line = _RESP.readline()
            if not _line:
                break
            _resp = _json.loads(_line)
            with _LOCK:
                if _resp.get("id") is None:
                    # An unidentifiable error fails ALL current waiters. Subsequent
                    # calls may proceed; late replies for failed calls are ignored.
                    _ec_fail_waiters(_resp.get("error") or "tool bridge reply missing id")
                    continue
                _waiter = _PENDING.pop(_resp["id"], None)
                if _waiter is not None:
                    _waiter[1] = _resp
                    _waiter[0].set()
    finally:
        with _LOCK:
            _CLOSED = True
            _ec_fail_waiters("tool bridge closed before responding")

def _ec_request(_kind, _name, kwargs):
    global _SEQ, _READER_STARTED, _CLOSED
    with _LOCK:
        if _CLOSED:
            raise RuntimeError("tool bridge closed before responding")
        _SEQ += 1
        _rid = _SEQ
        _frame = _json.dumps({"id": _rid, "type": _kind, "tool": _name, "args": kwargs}) + "\n"
        _waiter = [_threading.Event(), None]
        _PENDING[_rid] = _waiter
        if not _READER_STARTED:
            _threading.Thread(target=_ec_read_replies, daemon=True).start()
            _READER_STARTED = True
        try:
            _REQ.write(_frame)
            _REQ.flush()
        except OSError:
            _CLOSED = True
            _ec_fail_waiters("tool bridge closed before responding")
    _waiter[0].wait()
    _resp = _waiter[1]
    if not _resp.get("ok"):
        raise RuntimeError(_resp.get("error") or ("tool '%s' failed" % _name))
    return _resp.get("result")

class _ToolProxies:
    """Attribute access returns a callable that RPCs the named tool."""
    def help(self, name=None):
        return _ec_request("help", name, {})
    def __getattr__(self, _name):
        def _proxy(**kwargs):
            return _ec_request("tool_call", _name, kwargs)
        _proxy.__name__ = _name
        return _proxy
    def __call__(self, _name, **kwargs):  # tools("name", **kw) also works
        return _ec_request("tool_call", _name, kwargs)

tools = _ToolProxies()

# ---- user script below ----
'''


def _build_runner_file(code: str) -> str:
    """Write prelude + user code + result epilogue to a temp .py file."""
    fd, path = tempfile.mkstemp(prefix="ec_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(_RUNNER_PRELUDE)
        f.write("\n")
        f.write(code)
        # Exceptions and sys.exit() (even sys.exit(0)) skip this epilogue.
        # default= only reaches unserialisable VALUES, so a non-str dict key (e.g.
        # a tuple) or a circular reference would still raise and fail the run with
        # exit 1 where a plain script exited 0; fall back to a best-effort string
        # so setting `result` never regresses a previously-succeeding script.
        f.write(
            '\n\nif "result" in globals():\n'
            '    try:\n'
            '        _ec_result_text = _json.dumps(result, default=str, ensure_ascii=False)\n'
            '    except (TypeError, ValueError):\n'
            '        _ec_result_text = _json.dumps(str(result), ensure_ascii=False)\n'
            '    with open(_os.environ["EC_RESULT_PATH"], "w", encoding="utf-8") as _ec_result_file:\n'
            '        _ec_result_file.write(_ec_result_text)\n'
        )
    return path


def _record_bridged_call(
    session_id: str,
    name: str,
    args: dict,
    result: str,
    duration_ms: int,
    success: bool,
    *,
    parent_call_id: str = "",
    script_sha256: str = "",
) -> None:
    """Bridged-call observability (ADR 0103 S3, #2807).

    Before this, a script's tool calls were INVISIBLE: direct ``ainvoke`` never
    passes the graph's audit middleware, so a run that fetched ten URLs left no
    audit rows and no per-tool metrics — the model couldn't see the intermediate
    payloads (the point), but neither could the operator (a hole). The ADR's
    ``via: ptc`` tag lands as a ``ptc:`` tool-name prefix — one greppable
    convention that also splits bridged from direct calls in the Prometheus
    per-tool series without a schema change anywhere. Best-effort: observability
    must never break the RPC loop.

    ``parent_call_id`` (the model's tool-call id for the owning ``execute_code``
    call) and ``script_sha256`` land on the row via ``extra`` so an operator can
    tell which script made each ``ptc:`` call — two runs in one session are no
    longer indistinguishable. Both are omitted when empty, so the one-shot path
    without an id and the session kernel (which doesn't pass them yet) keep the
    row shape they had.

    ``args`` and ``result`` are run through the graph audit middleware's
    ``redact()`` before they are logged — the direct ``ainvoke`` bridge never
    passes ``AuditMiddleware``, which is the one place model-path tool rows get
    redacted, so a secret a script hands a bridged tool (or that a tool echoes
    back) would otherwise persist here in plaintext. This is the same redactor,
    in the same order (clip the result to 200, then redact), so the ``ptc:`` row
    is at credential parity with the model-path row.
    """
    try:
        from graph.middleware.redaction import redact
        from observability import metrics
        from observability.audit import audit_logger

        tagged = f"ptc:{name}"
        extra: dict[str, Any] = {}
        if parent_call_id:
            extra["parent_call_id"] = parent_call_id
        if script_sha256:
            extra["script_sha256"] = script_sha256
        audit_logger.log(
            session_id=session_id or "ptc",
            tool=tagged,
            args=redact(args),
            result_summary=redact((result or "")[:200]),
            duration_ms=duration_ms,
            success=success,
            extra=extra or None,
        )
        metrics.record_tool_call(tagged, success, duration_ms / 1000.0)
    except Exception:  # noqa: BLE001 — observability never breaks the bridge
        log.debug("[execute_code] bridged-call audit failed", exc_info=True)


# A one-shot script's full text is kept in its own ``ptc:script`` audit row (below)
# rather than in a tool's args — _sanitize_args clips every argument at 500 chars,
# which would lose a long script. Capped so a runaway script can't bloat the log.
_SCRIPT_CODE_CAP = 64 * 1024  # 64 KB


def _record_script(session_id: str, parent_call_id: str, script_sha256: str, code: str) -> None:
    """Record the full script behind a one-shot ``execute_code`` run as a single
    ``ptc:script`` audit row (the companion to :func:`_record_bridged_call`).

    It carries the same ``parent_call_id`` and ``script_sha256`` the run's bridged
    ``ptc:`` rows carry, so a ``ptc:`` row is never an orphan: an operator reading
    the audit log can find the exact script that made each bridged call and read it
    in full. The script rides ``extra['code']`` (merged into the row verbatim, not
    through the 500-char ``_sanitize_args``); it is capped at ``_SCRIPT_CODE_CAP``
    with ``code_truncated: true`` when cut. Best-effort like the bridged-call row —
    observability must never break the run; empty id/digest are omitted.

    The script is run through the graph audit middleware's ``redact()`` first — a
    one-shot run never passes ``AuditMiddleware`` (the model-path redactor), so a
    credential hardcoded in the script (a ``Bearer`` token, an ``API_KEY = "…"``
    assignment) would otherwise be persisted here in plaintext. Redacting before
    the cap keeps a secret from surviving on a truncation boundary.
    """
    try:
        from graph.middleware.redaction import redact
        from observability.audit import audit_logger

        extra: dict[str, Any] = {}
        if parent_call_id:
            extra["parent_call_id"] = parent_call_id
        if script_sha256:
            extra["script_sha256"] = script_sha256
        code = str(redact(code or ""))
        if len(code) > _SCRIPT_CODE_CAP:
            code = code[:_SCRIPT_CODE_CAP]
            extra["code_truncated"] = True
        extra["code"] = code
        audit_logger.log(
            session_id=session_id or "ptc",
            tool="ptc:script",
            args={},
            result_summary="",
            duration_ms=0,
            success=True,
            extra=extra,
        )
    except Exception:  # noqa: BLE001 — observability never breaks the run
        log.debug("[execute_code] script audit failed", exc_info=True)


def _deny_reason(name: str, gate, fence) -> str | None:
    """Binding-path parity for a bridged call (ADR 0103 S4, #2807): the same
    pre-execution checks a model-issued call meets, in the same order the graph
    runs them — the subagent fence (per-turn state), then the enforcement gate
    (config denylist + rate limits). A bridged call is never a second, softer
    path to a tool; it is the same path with a different caller."""
    if fence is not None and name not in fence:
        # Same reason text as SubagentFenceMiddleware._deny_reason — the script
        # (and the model reading its stderr) sees the identical policy message.
        return (
            f"tool '{name}' is outside this turn's tool allowlist "
            f"({', '.join(sorted(fence))}) — work within the allowed tools."
        )
    if gate is not None:
        return gate(name)
    return None


#: A run stops emitting ``ptc_call`` events after this many bridged calls, so a
#: script that loops over thousands of calls cannot flood the chat stream. Later
#: calls still run and are still audited; they just have no live card.
_PTC_EVENT_MAX_CALLS = 200
#: Chars of a bridged call's output carried on its ``end`` event (``output_chars``
#: is the full length).
_PTC_EVENT_OUTPUT_CAP = 2000


def _ptc_event_sink():
    """An async sink that emits each payload as a ``ptc_call`` custom event under
    the CALLING ``execute_code`` run, or None outside one.

    The run config is captured NOW, in the tool body: bridged calls are served on
    the RPC reader's own tasks (and, for a session kernel, on a reader task that
    outlives the call), whose context holds no LangChain run, so a bare
    ``adispatch_custom_event`` there would have no parent run to attach to."""
    try:
        from langchain_core.callbacks import adispatch_custom_event
        from langchain_core.runnables.config import ensure_config

        config = ensure_config()
    except Exception:  # noqa: BLE001 — no run context (a unit test, a CLI call): no live view
        return None
    if not config.get("callbacks"):
        return None

    async def _sink(payload: dict) -> None:
        await adispatch_custom_event("ptc_call", payload, config=config)

    return _sink


class _PtcEvents:
    """The ``ptc_call`` event stream of ONE ``execute_code`` run.

    Holds the run's sink, its ``parent_call_id`` (the execute_code call's model
    tool-call id — the same id the audit rows carry) and the per-run call counter
    that makes each bridged call's event id unique. Emitting is best-effort: a
    sink failure is logged at debug and never reaches the bridged call."""

    def __init__(self, sink, parent_call_id: str):
        self._sink = sink
        self._parent_call_id = parent_call_id or ""
        self._calls = 0

    async def _emit(self, payload: dict) -> None:
        try:
            await self._sink(payload)
        except Exception:  # noqa: BLE001 — the live view never changes a tool result
            log.debug("[execute_code] ptc_call event emit failed", exc_info=True)

    async def start(self, name: str, args: dict) -> str | None:
        """Emit the ``start`` event; return the call's event id, or None when this
        call emits nothing (no sink, no parent call id, or past the per-run cap)."""
        if self._sink is None or not self._parent_call_id:
            return None
        self._calls += 1
        if self._calls > _PTC_EVENT_MAX_CALLS:
            return None
        call_id = f"{self._parent_call_id}:ptc:{self._calls}"
        await self._emit({
            "phase": "start",
            "id": call_id,
            "name": name,
            "args": args,
            "parent_call_id": self._parent_call_id,
        })
        return call_id

    async def end(self, call_id: str | None, name: str, output: str, *, error: bool, duration_ms: int) -> None:
        """Emit the ``end`` event for a call whose ``start`` was emitted."""
        if call_id is None:
            return
        await self._emit({
            "phase": "end",
            "id": call_id,
            "name": name,
            "output": output[:_PTC_EVENT_OUTPUT_CAP],
            "output_chars": len(output),
            "error": error,
            "duration_ms": duration_ms,
            "parent_call_id": self._parent_call_id,
        })


async def _invoke_bridged(
    name: str,
    args: dict,
    tool_map: dict,
    *,
    session_id: str,
    gate,
    fence,
    parent_call_id: str = "",
    script_sha256: str = "",
    ptc_events: "_PtcEvents | None" = None,
) -> tuple[bool, str]:
    """Run one bridged tool call under call-bound policy and record its audit row.

    The single home for the binding-path contract (ADR 0103 S4): resolve the tool
    in the allowlist, apply the fence then the enforcement gate, invoke the real
    async tool, and write the ``ptc:`` audit/metrics row (ADR 0103 S3). Returns
    ``(ok, payload)`` where ``payload`` is the result text on success or the error
    string on failure — the one-shot service (:func:`_service_rpc`) and the session
    kernel (``kernel.py``) both frame it onto their own wire, so a bridged call is
    the same path with the same policy whichever harness runs it.

    ``parent_call_id``/``script_sha256`` (empty by default) attribute the row to the
    ``execute_code`` call and script that issued it; they thread straight onto the
    audit row. The kernel passes neither yet, so its rows are unchanged.

    ``ptc_events`` (the owning run's :class:`_PtcEvents`, or None) gets a ``start``
    event before the call — a policy denial included — and an ``end`` event after
    it, so the console can show the call live. It never changes the result."""
    import time

    tool = tool_map.get(name)
    if tool is None:
        return False, f"tool '{name}' not available"
    denied = _deny_reason(name, gate, fence)
    if denied:
        # Same "Blocked by policy:" prefix the enforcement/fence ToolMessages carry —
        # and the denial is a RECORDED bridged call (success=False), so policy blocks
        # are as observable on this path as on the direct one.
        err = f"Blocked by policy: {denied}"
        _record_bridged_call(
            session_id, name, args, err, 0, False, parent_call_id=parent_call_id, script_sha256=script_sha256
        )
        if ptc_events is not None:
            event_id = await ptc_events.start(name, args)
            await ptc_events.end(event_id, name, err, error=True, duration_ms=0)
        return False, err
    event_id = await ptc_events.start(name, args) if ptc_events is not None else None
    started = time.monotonic()
    try:
        result = await tool.ainvoke(args)
        text = result if isinstance(result, str) else str(result)
        duration_ms = int((time.monotonic() - started) * 1000)
        _record_bridged_call(
            session_id,
            name,
            args,
            text,
            duration_ms,
            True,
            parent_call_id=parent_call_id,
            script_sha256=script_sha256,
        )
        if ptc_events is not None:
            await ptc_events.end(event_id, name, text, error=False, duration_ms=duration_ms)
        return True, text
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        duration_ms = int((time.monotonic() - started) * 1000)
        _record_bridged_call(
            session_id,
            name,
            args,
            err,
            duration_ms,
            False,
            parent_call_id=parent_call_id,
            script_sha256=script_sha256,
        )
        if ptc_events is not None:
            await ptc_events.end(event_id, name, err, error=True, duration_ms=duration_ms)
        return False, err


# The request id at the head of a child frame (`{"id": 7, ...`) — the prelude's
# json.dumps puts it first — so even an unparseable frame can be answered by id.
_FRAME_ID_RE = re.compile(rb'\s*\{\s*"id"\s*:\s*(\d+)')


_MAX_PARALLEL_CALLS = 8


async def _service_rpc(
    req_reader: asyncio.StreamReader,
    resp_writer,
    tool_map: dict,
    *,
    session_id: str = "",
    gate=None,
    fence=None,
    parent_call_id: str = "",
    script_sha256: str = "",
    ptc_sink=None,
):
    """Read RPC requests from the child, invoke real tools, write back results.

    ``parent_call_id``/``script_sha256`` (empty by default) are attributed to every
    bridged call this one-shot run records, so its ``ptc:`` rows name the script.
    ``ptc_sink`` (an async ``payload -> None``, or None) receives this run's
    ``ptc_call`` start/end events."""
    ptc_events = _PtcEvents(ptc_sink, parent_call_id)
    semaphore = asyncio.Semaphore(_MAX_PARALLEL_CALLS)
    write_lock = asyncio.Lock()
    pending: set[asyncio.Task] = set()
    service_task = asyncio.current_task()

    async def respond(resp):
        async with write_lock:
            try:
                resp_writer.write((json.dumps(resp) + "\n").encode())
                await resp_writer.drain()
            except Exception:
                service_task.cancel()  # child gone: stop dispatch and cancel outstanding calls

    async def dispatch(msg):
        async with semaphore:
            rid, name, args = msg.get("id"), msg.get("tool"), msg.get("args") or {}
            ok, payload = await _invoke_bridged(
                name,
                args,
                tool_map,
                session_id=session_id,
                gate=gate,
                fence=fence,
                parent_call_id=parent_call_id,
                script_sha256=script_sha256,
                ptc_events=ptc_events,
            )
            resp = {"id": rid, "ok": ok, ("result" if ok else "error"): payload}
            await respond(resp)

    try:
        while True:
            line = await req_reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode())
                if not isinstance(msg, dict):
                    raise ValueError(f"expected a JSON object, got {type(msg).__name__}")
                if msg.get("type") == "help":
                    await respond({"id": msg.get("id"), "ok": True, "result": _tool_help(tool_map, msg.get("tool"))})
                    continue
                if not isinstance(msg.get("tool"), str):
                    raise ValueError("expected a tool name string")
            except Exception as exc:
                # Answer every malformed frame, recovering its id when possible.
                log.warning("[execute_code] bad RPC frame: %s", exc)
                m = _FRAME_ID_RE.match(line)
                await respond({
                    "id": int(m.group(1)) if m else None,
                    "ok": False,
                    "error": f"malformed tool-bridge request: {exc}",
                })
                continue
            task = asyncio.create_task(dispatch(msg))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _resolve_child_interpreter() -> str | None:
    """The interpreter that runs the child script.

    Source/venv runs: this process's own interpreter — its site-packages ARE the
    child's library surface, as they always have been. Packaged desktop (ADR 0094):
    ``sys.executable`` is the frozen server binary (``<binary> -u <script>`` would
    relaunch the server, not run the script), so spawn the managed CPython instead —
    or None when it isn't provisioned yet, and ``run_code`` speaks the install path."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    from infra.python_runtime import managed_python_exe

    exe = managed_python_exe()
    return str(exe) if exe is not None else None


# The managed Python runtime isn't provisioned yet (packaged desktop, ADR 0094).
# Returned as a tool RESULT (not a raise) so the tool_call is answered and the thread
# can't be left with a dangling tool_call that 400s every later turn — and it points
# at the one-time fix instead of a dead end. Shared by the one-shot path and the kernel.
_NO_RUNTIME_MESSAGE = (
    "Error: execute_code needs the managed Python runtime, which isn't provisioned "
    "on this machine yet. Install it once from Settings ▸ Tools (a ~35 MB, "
    "hash-verified download) or run `protoagent runtime install-python`, then retry."
)


# --- stdout spill (#3701) ----------------------------------------------------
# When a script's stdout overflows ``truncate``, the head is returned inline and
# the FULL output is written to a scratch file under the instance's plugin store,
# so an evidence sweep across a whole repo isn't forced to re-run with narrower
# prints (the overflow used to be dropped). Bounded so a runaway script can't
# fill the disk (per-file cap) or leave litter (TTL prune on every write); a
# failed write is swallowed and falls back to the plain truncation marker — the
# spill never fails the run.
_SPILL_MAX_BYTES = 5 * 1024 * 1024  # per-file cap; past this the spill itself is truncated
_SPILL_TTL_SECONDS = 24 * 60 * 60  # prune spill files older than this on each write


def _prune_old_spills(spill_dir: Path, now: float) -> None:
    """Delete spill files older than the TTL. Best-effort — a file that vanishes
    or can't be stat'd between glob and unlink is skipped, never raised."""
    cutoff = now - _SPILL_TTL_SECONDS
    for old in spill_dir.glob("ec-*.txt"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()


def _spill_stdout(full: str, truncate: int, *, session_id: str) -> str:
    """Write ``full`` stdout to a per-session scratch file and return the
    truncated head plus a marker naming the full size, the absolute spill path,
    and how to read it. Raises on any failure so the caller can fall back to the
    plain truncation marker — a broken/read-only/over-quota store never fails the run.
    """
    import time as _time

    from graph.sdk import plugin_store

    spill_dir = plugin_store(subdir="spill", plugin_id="execute_code")
    _prune_old_spills(spill_dir, _time.time())

    data = full.encode("utf-8", errors="replace")
    capped = ""
    if len(data) > _SPILL_MAX_BYTES:
        data = data[:_SPILL_MAX_BYTES]
        capped = f" (spill capped at {_SPILL_MAX_BYTES} bytes; the tail was dropped)"

    # Per-session, per-run unique name. The session id is sanitised to a safe stem
    # (it comes from InjectedState, but the store is on disk) and a random suffix
    # keeps concurrent runs in one session from colliding.
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:40] or "nosession"
    path = spill_dir / f"ec-{sid}-{_secrets.token_hex(8)}.txt"
    path.write_bytes(data)

    # The recovery read must return the part NOT shown, not the head again. A read
    # that starts at 0 would replay full[:truncate] and re-spill (its own stdout
    # overflows ``truncate`` too), so the marker directs a *paged* read that starts
    # past the head and stays within one page — offset ``truncate`` to ``2*truncate``,
    # then advance by ``truncate`` — so each follow-up returns fresh tail and never
    # spills again. read_file only reaches the path when the store is a managed
    # project; execute_code (a subprocess) can always open it.
    next_end = 2 * truncate
    return (
        full[:truncate]
        + f"\n\n…[output truncated: showing {truncate} of {len(full)} chars. "
        + f"Full stdout written to {path}{capped}. "
        + "To recover the part NOT shown, read_file can page it if that path is inside a "
        + "managed project; otherwise use a bounded read that STARTS PAST THE HEAD (a read "
        + "from the start just replays this same head and spills again) — e.g. execute_code: "
        + f'print(open(r"{path}", encoding="utf-8", errors="replace").read()[{truncate}:{next_end}]) '
        + f"returns the next {truncate} chars; advance both offsets by {truncate} to keep "
        + "paging (each page stays within the truncate limit, so it won't spill again).]"
    )


def _truncate_or_spill(out: str, truncate: int, *, session_id: str) -> str:
    """Return ``out`` unchanged if within ``truncate``; otherwise spill the full text
    to a scratch file and return the head plus a recovery marker (#3701), falling back
    to a plain truncation marker if the store is broken. Shared by the one-shot path
    and the session kernel so both overflow the same way."""
    if len(out) <= truncate:
        return out
    try:
        return _spill_stdout(out, truncate, session_id=session_id)
    except Exception:  # noqa: BLE001 — a broken store falls back, never fails the run
        log.debug("[execute_code] stdout spill failed; using plain truncation", exc_info=True)
        return out[:truncate] + f"\n\n…[truncated to {truncate} chars]"


async def run_code(
    code: str,
    tool_map: dict,
    *,
    timeout: float = 30.0,
    truncate: int = 6000,
    session_id: str = "",
    gate=None,
    fence=None,
    parent_call_id: str = "",
    script_sha256: str = "",
    ptc_sink=None,
) -> str:
    """Run ``code`` with a tool-RPC bridge; return stdout and any JSON result.

    ``session_id`` attributes the run's bridged tool calls in the audit log
    (ADR 0103 S3); empty degrades to the literal ``"ptc"`` bucket. ``gate``
    (``name -> deny-reason | None``, from the enforcement config) and ``fence``
    (the turn's subagent allowlist, or None) apply the model-path policy checks
    to every bridged call (S4 binding parity).

    ``parent_call_id`` (the owning ``execute_code`` call's model tool-call id) and
    ``script_sha256`` (the script's digest) attribute each bridged ``ptc:`` row to
    the script that made it; both empty by default so a direct call is unaffected.

    ``ptc_sink`` (an async ``payload -> None``, or None) receives a ``ptc_call``
    start and end event for each bridged call, tagged with ``parent_call_id``; with
    no sink or no ``parent_call_id`` nothing is emitted.
    """
    interpreter = _resolve_child_interpreter()
    if interpreter is None:
        return _NO_RUNTIME_MESSAGE
    path = _build_runner_file(code)
    # A loopback TCP server the child connects back to for tool-RPC. TCP (not
    # anonymous pipes + fd inheritance) is what makes the bridge portable to Windows.
    # The per-run token gates the ephemeral port so no other local process can drive
    # our tool loop — the child must present it as its first line.
    token = _secrets.token_hex(16)
    loop = asyncio.get_event_loop()
    authed: asyncio.Future = loop.create_future()

    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await reader.readline()
        except Exception:  # noqa: BLE001 — a failed handshake just drops the socket
            first = b""
        # Wrong token, or a second connection after we're already serving one → refuse.
        if authed.done() or first.decode(errors="replace").strip() != token:
            with contextlib.suppress(Exception):
                writer.close()
            return
        authed.set_result((reader, writer))

    server = await asyncio.start_server(_on_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # Scrubbed env — no gateway keys / auth tokens reach the script. On Windows a
    # freshly spawned python.exe still needs a few OS-essential vars to start
    # (SystemRoot above all); the loop is a no-op on POSIX.
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "EC_PORT": str(port),
        "EC_TOKEN": token,
    }
    if os.name == "nt":
        for _k in ("SystemRoot", "TEMP", "TMP", "COMSPEC", "PATHEXT"):
            if _k in os.environ:
                child_env[_k] = os.environ[_k]

    proc = None
    service = None
    result_dir = None
    try:
        result_dir = tempfile.TemporaryDirectory(prefix="ec_result_")
        result_path = Path(result_dir.name) / "result.json"
        child_env["EC_RESULT_PATH"] = str(result_path)
        proc = await asyncio.create_subprocess_exec(
            interpreter,
            "-u",
            path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            **group_kwargs(),  # ADR 0098: anchor the tree so a timeout kills grandchildren too
        )
        # Owned until reaped (#3428) — an exit mid-script takes the tree down with it.
        track_tree(proc.pid)

        async def _serve() -> None:
            reader, writer = await authed
            try:
                await _service_rpc(
                    reader,
                    writer,
                    tool_map,
                    session_id=session_id,
                    gate=gate,
                    fence=fence,
                    parent_call_id=parent_call_id,
                    script_sha256=script_sha256,
                    ptc_sink=ptc_sink,
                )
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        service = asyncio.ensure_future(_serve())
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await akill_tree(proc)  # ADR 0098: the whole tree, not just the direct child
            with contextlib.suppress(Exception):
                await proc.wait()
            return f"Error: execute_code timed out after {timeout}s (process killed)."
        finally:
            if service is not None:
                service.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await service

        out = (stdout or b"").decode(errors="replace").strip()
        err = (stderr or b"").decode(errors="replace").strip()
        if proc.returncode != 0:
            detail = err or "(no stderr)"
            body = f"Error: script exited with code {proc.returncode}.\n{detail}"
            if out:
                body += f"\n\n--- stdout before failure ---\n{out}"
            out = body
        else:
            if result_path.exists():
                result_text = result_path.read_text(encoding="utf-8")
                out = f"{out}\n--- result ---\n{result_text}" if out else result_text
            if not out:
                out = "(script produced no stdout)"

        return _truncate_or_spill(out, truncate, session_id=session_id)
    finally:
        if proc is not None:
            if proc.returncode is not None:
                untrack_tree(proc.pid)
            else:
                untrack_when_reaped(proc)  # cancelled mid-run: forget it once it finishes
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        with contextlib.suppress(OSError):
            os.unlink(path)
        if result_dir is not None:
            result_dir.cleanup()


# Never bridgeable, regardless of configuration (ADR 0103 D3, #2807):
# - HITL tools: a LangGraph interrupt cannot park a subprocess mid-script — the
#   same structural reason subagents hard-deny them; from the bridge it would
#   surface as an opaque error after wedging the run.
# - task/task_batch: nested delegation from model-written code is out of scope
#   (D6) — a script that can spawn subagents is an escalation, not a batch call.
# - execute_code itself: no recursion (as before).
_NEVER_BRIDGED = frozenset({"ask_human", "request_user_input", "task", "task_batch", "execute_code"})

# The curated read-mostly DEFAULT bridge set (ADR 0103 D3): what the script can
# call when the operator hasn't named an allowlist. Read-only, individually
# capped tools — batching reads is the pattern PTC exists for. An operator
# widens it by naming tools in the plugin's ``tools`` config (explicit intent;
# _NEVER_BRIDGED still applies). The old default exposed EVERY registered tool,
# write tools and delegation included — the allowlist is the security posture,
# and "everything" isn't a posture.
_DEFAULT_BRIDGE_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "find_files",
        "search_files",
        "fetch_url",
        "web_search",
        "memory_search",
        "knowledge_search",
        "memory_list",
        "current_time",
    }
)


def _tool_signature(t) -> str:
    """One legible line per bridged tool (ADR 0103 S2, #2807): a call signature
    built from the tool's real schema plus its description's first line — so the
    model writes ``tools.read_file(project=..., path=...)`` instead of guessing
    kwargs against a name-only proxy. Injected params never appear (``.args``
    already excludes them); anything unreadable degrades to ``name(…)``."""
    try:
        params = []
        for pname, meta in (t.args or {}).items():
            if isinstance(meta, dict) and "default" in meta:
                params.append(f"{pname}={meta['default']!r}")
            else:
                params.append(pname)
        first = ((t.description or "").strip().splitlines() or [""])[0][:110]
        sig = f"tools.{t.name}({', '.join(params)})"
        return f"{sig} — {first}" if first else sig
    except Exception:  # noqa: BLE001 — a weird schema must not break tool build
        return f"tools.{t.name}(…)"


def _tool_help(tool_map: dict, name=None):
    """Return bridge metadata without invoking, enforcing or auditing a tool call."""
    if name is None:
        return sorted(tool_map)
    t = tool_map.get(name) if isinstance(name, str) else None
    if t is None:
        return f"Tool {name!r} is not bridged."
    return f"{_tool_signature(t)}\n{t.description or ''}"


# Description budget: a wide explicit allowlist must not balloon the (cached,
# but still token-bearing) tool schema — past this many, the rest list by name.
_MAX_SIGNATURE_LINES = 25


def _build_enforcement_gate(graph_config):
    """The bridge's copy of ``EnforcementMiddleware._enforce`` (ADR 0103 S4),
    built from the SAME config fields the graph wires the middleware from —
    exact-name denylist first, then the sliding-window rate limit. One honest
    deviation, by construction: the middleware instance is built AFTER the
    late-tools seam runs, so the limiter here is a separate window over the
    same limits — bridged and direct calls each get the configured budget
    rather than sharing one. The denylist (the security-relevant half) is
    exact parity. Core wires no predicate; a fork that adds one should extend
    this gate too."""
    if graph_config is None or not getattr(graph_config, "enforcement_enabled", False):
        return None
    denied = set(getattr(graph_config, "enforcement_disallowed_tools", None) or ())
    limits = getattr(graph_config, "enforcement_rate_limits", None)
    if not denied and not limits:
        return None
    from enforcement.rate_limiter import RateLimiter

    limiter = RateLimiter(limits) if limits else None

    def _gate(name: str) -> str | None:
        if name in denied:
            return f"Tool '{name}' is disabled by policy."
        if limiter is not None:
            allowed, reason = limiter.check(name)
            if not allowed:
                return reason
        return None

    return _gate


class _ExecuteCodeTool(StructuredTool):
    """Keep direct calls usable alongside model tool-call id injection."""

    def _parse_input(self, tool_input: str | dict[str, Any], tool_call_id: str | None) -> str | dict[str, Any]:
        # LangChain rejects a missing InjectedToolCallId before the coroutine's
        # default can apply. Supply the empty id only for argument validation;
        # arun keeps its original None so direct calls still return plain text.
        return super()._parse_input(tool_input, tool_call_id if tool_call_id is not None else "")


def build_execute_code_tool(
    all_tools: list,
    *,
    tools=None,
    timeout: float = 30.0,
    truncate: int = 6000,
    graph_config=None,
    session_state: bool = False,
):
    """Build the ``execute_code`` LangChain tool over an allowlist of tools.

    ``all_tools`` is the agent's full toolset. ``tools`` empty/None exposes the
    curated read-mostly default set (ADR 0103 D3); a configured list exposes
    exactly those names. Either way ``_NEVER_BRIDGED`` is subtracted — HITL and
    delegation are structurally unbridgeable, not policy preferences. ``timeout``
    (seconds) and ``truncate`` (chars of stdout) come from the plugin's
    ``execute_code`` config section. ``graph_config`` (the full graph config the
    late-tools seam passes) supplies the enforcement fields for S4 parity.

    ``session_state`` (ADR 0134 D1, experimental, default off) keeps a long-lived
    kernel per session so variables carry across calls; with it off the tool is
    unchanged — a fresh subprocess each call, and ``fresh`` is a no-op.
    """
    from graph.tool_renames import migrate_tool_names

    # ADR 0125: a caller-supplied list may still name the removed memory tools.
    allow = set(migrate_tool_names(tools, source="execute_code.tools")) or set(_DEFAULT_BRIDGE_TOOLS)
    allow -= _NEVER_BRIDGED
    tool_map = {t.name: t for t in all_tools if t.name in allow}
    enforcement_gate = _build_enforcement_gate(graph_config)
    ordered = [tool_map[n] for n in sorted(tool_map)]
    sig_lines = [_tool_signature(t) for t in ordered[:_MAX_SIGNATURE_LINES]]
    overflow = [t.name for t in ordered[_MAX_SIGNATURE_LINES:]]
    if overflow:
        sig_lines.append(f"(+{len(overflow)} more, same calling shape: {', '.join(overflow)})")
        sig_lines.append("Use tools.help(name) for signatures and full descriptions.")
    available = "\n".join(f"  {line}" for line in sig_lines) or "  (none)"

    if session_state:
        # State persists across calls in the session (ADR 0134 D1); explain the
        # carry-over, when it's lost, and the `fresh` escape hatch.
        state_text = (
            "and a scrubbed environment (no credentials). State PERSISTS across calls in "
            "this session: variables, imports and functions you define carry over to your "
            "next execute_code call, so fetch a large result once, keep it in a variable, "
            "and filter it again later without re-fetching or re-printing it. State is lost "
            "when a call times out or the kernel restarts, and never survives a server "
            "restart; the first call in a fresh kernel is prefixed `new kernel: no earlier "
            "state`. Pass `fresh=True` to restart this session's kernel and start from a "
            "clean slate. "
        )
    else:
        state_text = "and a scrubbed environment (no credentials), fresh each call (no state persists between runs). "

    description = (
        "Run a Python script in a sandboxed subprocess and get its stdout — a "
        "general-purpose code interpreter. Two main uses:\n"
        "1. Computation/data work — parse, transform, compute, filter; anything "
        "Python (stdlib) can do.\n"
        "2. Programmatic tool-calling — collapse a multi-step tool chain into one "
        "turn: call several tools, loop/filter/combine their results in code, and "
        "print() only the final answer (you read just the stdout, not every "
        "intermediate payload).\n\n"
        "Call tools via the injected `tools` object, e.g.:\n"
        "    results = [tools.web_search(query=q) for q in queries]\n"
        "    print('\\n\\n'.join(results)[:2000])\n\n"
        f"Each tool returns a string. Available tools (call signatures):\n{available}\n\n"
        f"tools.* calls are thread-safe and run concurrently (up to {_MAX_PARALLEL_CALLS} per run). "
        "Use concurrent.futures.ThreadPoolExecutor for independent calls.\n\n"
        f"The script runs in an isolated subprocess with a {timeout:.0f}s timeout "
        + state_text
        + "You may set `result = {...}` to return structured "
        "data as JSON instead of printing. If you also print, stdout precedes a "
        "`--- result ---` line and the JSON. Output over the truncate limit is returned as a head plus a marker "
        "naming a scratch file that holds the FULL output (read it if you need the rest). "
        "Exceptions and a non-zero exit are reported back to you."
    )

    async def execute_code(
        code: str,
        fresh: bool = False,
        state: Annotated[Any, InjectedState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        if not code or not code.strip():
            return "Error: execute_code called with empty code."
        # Session attribution for the run's bridged calls (ADR 0103 S3) — the
        # InjectedState lane, not current_session_id() (empty inside tool bodies).
        # Excluded from the model-facing schema; None on a direct ainvoke.
        sid = str(state.get("session_id") or "") if isinstance(state, dict) else ""
        # The model's tool-call id for THIS execute_code call (InjectedToolCallId —
        # kept out of the model-facing schema, like `state`); it links each bridged
        # ptc: row and the ptc:script row back to this one call, so two runs in one
        # session are distinguishable. Empty when invoked without a tool-call id.
        # Digest the script once so the same value tags every row this run writes.
        script_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        # S4 fence parity: a fenced background run that allowlists execute_code
        # must not bridge past the fence — read the turn's stamped allowlist from
        # the same state channel SubagentFenceMiddleware reads, and apply it to
        # every bridged call. No fence on the turn → None → no check.
        raw_fence = state.get("subagent_fence") if isinstance(state, dict) else None
        fence = frozenset(raw_fence) if raw_fence else None
        # Live view: each bridged call is re-emitted as a `ptc_call` custom event
        # under THIS run. The run config must be captured here, in the tool body —
        # the bridge serves calls on tasks that hold no LangChain run.
        ptc_sink = _ptc_event_sink()
        try:
            # ADR 0134 D1: with session_state on and a real session, run in the
            # session's long-lived kernel so state carries across calls. An empty
            # session_id (direct ainvoke, unattributed turn) stays on the one-shot
            # path — there is no session to key a kernel by. `fresh` is kernel-only.
            if session_state and sid:
                from .kernel import run_in_kernel

                return await run_in_kernel(
                    code,
                    tool_map,
                    timeout=timeout,
                    truncate=truncate,
                    session_id=sid,
                    gate=enforcement_gate,
                    fence=fence,
                    fresh=fresh,
                    parent_call_id=tool_call_id,
                    script_sha256=script_sha256,
                    ptc_sink=ptc_sink,
                )
            # One-shot run: keep the full script in its own ptc:script audit row
            # (once per run) so every bridged ptc: row this run writes can be traced
            # back to it and read whole, then attribute each bridged row to this call.
            _record_script(sid, tool_call_id, script_sha256, code)
            return await run_code(
                code,
                tool_map,
                timeout=timeout,
                truncate=truncate,
                session_id=sid,
                gate=enforcement_gate,
                fence=fence,
                parent_call_id=tool_call_id,
                script_sha256=script_sha256,
                ptc_sink=ptc_sink,
            )
        except Exception as exc:
            log.exception("[execute_code] harness failure")
            return f"Error: execute_code harness failed: {type(exc).__name__}: {exc}"

    return _ExecuteCodeTool.from_function(coroutine=execute_code, name="execute_code", description=description)
