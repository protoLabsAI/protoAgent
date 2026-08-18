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

Security posture
----------------
Opt-in (``plugins.enabled: [execute_code]``); runs **arbitrary model-authored
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
import json
import logging
import os
import secrets as _secrets
import sys
import tempfile
from typing import Annotated, Any

from langgraph.prebuilt import InjectedState

from infra.proc import akill_tree, group_kwargs

log = logging.getLogger(__name__)

# Prelude prepended to the user's script in the child process. Sets up the
# `tools` proxy object that bridges calls back to the parent over a loopback TCP
# socket whose port + per-run token are named in the environment. A socket
# (not anonymous pipes + fd inheritance) is what makes the bridge portable to
# Windows, where fd numbers don't survive spawn and anon pipes aren't
# Proactor-drivable (ADR 0098 / #2449). Kept dependency-free (stdlib only).
_RUNNER_PRELUDE = r'''
import os as _os, sys as _sys, json as _json, socket as _socket

_sock = _socket.create_connection(("127.0.0.1", int(_os.environ["EC_PORT"])))
_REQ = _sock.makefile("w")   # child -> parent
_RESP = _sock.makefile("r")  # parent -> child
_REQ.write(_os.environ["EC_TOKEN"] + "\n")  # authenticate this run's connection
_REQ.flush()
_SEQ = 0

def _ec_call(_name, **kwargs):
    global _SEQ
    _SEQ += 1
    _rid = _SEQ
    _REQ.write(_json.dumps({"id": _rid, "tool": _name, "args": kwargs}) + "\n")
    _REQ.flush()
    _line = _RESP.readline()
    if not _line:
        raise RuntimeError("tool bridge closed before responding")
    _resp = _json.loads(_line)
    if not _resp.get("ok"):
        raise RuntimeError(_resp.get("error") or ("tool '%s' failed" % _name))
    return _resp.get("result")

class _ToolProxies:
    """Attribute access returns a callable that RPCs the named tool."""
    def __getattr__(self, _name):
        def _proxy(**kwargs):
            return _ec_call(_name, **kwargs)
        _proxy.__name__ = _name
        return _proxy
    def __call__(self, _name, **kwargs):  # tools("name", **kw) also works
        return _ec_call(_name, **kwargs)

tools = _ToolProxies()

# ---- user script below ----
'''


def _build_runner_file(code: str) -> str:
    """Write prelude + user code to a temp .py file; return its path."""
    fd, path = tempfile.mkstemp(prefix="ec_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(_RUNNER_PRELUDE)
        f.write("\n")
        f.write(code)
    return path


def _record_bridged_call(session_id: str, name: str, args: dict, result: str, duration_ms: int, success: bool) -> None:
    """Bridged-call observability (ADR 0103 S3, #2807).

    Before this, a script's tool calls were INVISIBLE: direct ``ainvoke`` never
    passes the graph's audit middleware, so a run that fetched ten URLs left no
    audit rows and no per-tool metrics — the model couldn't see the intermediate
    payloads (the point), but neither could the operator (a hole). The ADR's
    ``via: ptc`` tag lands as a ``ptc:`` tool-name prefix — one greppable
    convention that also splits bridged from direct calls in the Prometheus
    per-tool series without a schema change anywhere. Best-effort: observability
    must never break the RPC loop.
    """
    try:
        from observability import metrics
        from observability.audit import audit_logger

        tagged = f"ptc:{name}"
        audit_logger.log(
            session_id=session_id or "ptc",
            tool=tagged,
            args=args,
            result_summary=(result or "")[:200],
            duration_ms=duration_ms,
            success=success,
        )
        metrics.record_tool_call(tagged, success, duration_ms / 1000.0)
    except Exception:  # noqa: BLE001 — observability never breaks the bridge
        log.debug("[execute_code] bridged-call audit failed", exc_info=True)


async def _service_rpc(req_reader: asyncio.StreamReader, resp_writer, tool_map: dict, *, session_id: str = ""):
    """Read RPC requests from the child, invoke real tools, write back results."""
    import time

    while True:
        line = await req_reader.readline()
        if not line:  # child closed the pipe (exited)
            return
        try:
            msg = json.loads(line.decode())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[execute_code] bad RPC frame: %s", exc)
            continue
        rid, name, args = msg.get("id"), msg.get("tool"), msg.get("args") or {}
        tool = tool_map.get(name)
        if tool is None:
            resp = {"id": rid, "ok": False, "error": f"tool '{name}' not available"}
        else:
            started = time.monotonic()
            try:
                result = await tool.ainvoke(args)
                text = result if isinstance(result, str) else str(result)
                resp = {"id": rid, "ok": True, "result": text}
                _record_bridged_call(session_id, name, args, text, int((time.monotonic() - started) * 1000), True)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                resp = {"id": rid, "ok": False, "error": err}
                _record_bridged_call(session_id, name, args, err, int((time.monotonic() - started) * 1000), False)
        try:
            resp_writer.write((json.dumps(resp) + "\n").encode())
            await resp_writer.drain()
        except Exception:
            return  # child gone


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


async def run_code(code: str, tool_map: dict, *, timeout: float = 30.0, truncate: int = 6000, session_id: str = "") -> str:
    """Run ``code`` in a child process with a tool-RPC bridge; return its stdout.

    ``session_id`` attributes the run's bridged tool calls in the audit log
    (ADR 0103 S3); empty degrades to the literal ``"ptc"`` bucket.
    """
    interpreter = _resolve_child_interpreter()
    if interpreter is None:
        # Refuse cleanly with a tool RESULT (so the tool_call is answered and the
        # thread can't be left with a dangling tool_call that 400s every later turn)
        # — and point at the one-time fix instead of a dead end (ADR 0094).
        return (
            "Error: execute_code needs the managed Python runtime, which isn't provisioned "
            "on this machine yet. Install it once from Settings ▸ Tools (a ~35 MB, "
            "hash-verified download) or run `protoagent runtime install-python`, then retry."
        )
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
    try:
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

        async def _serve() -> None:
            reader, writer = await authed
            try:
                await _service_rpc(reader, writer, tool_map, session_id=session_id)
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
        elif not out:
            out = "(script produced no stdout)"

        if len(out) > truncate:
            out = out[:truncate] + f"\n\n…[truncated to {truncate} chars]"
        return out
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        with contextlib.suppress(OSError):
            os.unlink(path)


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
        "memory_recall",
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


# Description budget: a wide explicit allowlist must not balloon the (cached,
# but still token-bearing) tool schema — past this many, the rest list by name.
_MAX_SIGNATURE_LINES = 25


def build_execute_code_tool(all_tools: list, *, tools=None, timeout: float = 30.0, truncate: int = 6000):
    """Build the ``execute_code`` LangChain tool over an allowlist of tools.

    ``all_tools`` is the agent's full toolset. ``tools`` empty/None exposes the
    curated read-mostly default set (ADR 0103 D3); a configured list exposes
    exactly those names. Either way ``_NEVER_BRIDGED`` is subtracted — HITL and
    delegation are structurally unbridgeable, not policy preferences. ``timeout``
    (seconds) and ``truncate`` (chars of stdout) come from the plugin's
    ``execute_code`` config section.
    """
    from langchain_core.tools import tool

    allow = set(tools or []) or set(_DEFAULT_BRIDGE_TOOLS)
    allow -= _NEVER_BRIDGED
    tool_map = {t.name: t for t in all_tools if t.name in allow}
    ordered = [tool_map[n] for n in sorted(tool_map)]
    sig_lines = [_tool_signature(t) for t in ordered[:_MAX_SIGNATURE_LINES]]
    overflow = [t.name for t in ordered[_MAX_SIGNATURE_LINES:]]
    if overflow:
        sig_lines.append(f"(+{len(overflow)} more, same calling shape: {', '.join(overflow)})")
    available = "\n".join(f"  {line}" for line in sig_lines) or "  (none)"

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
        f"The script runs in an isolated subprocess with a {timeout:.0f}s timeout "
        "and a scrubbed environment (no credentials), fresh each call (no state "
        "persists between runs). Only stdout is returned; write your result with "
        "print(). Exceptions and a non-zero exit are reported back to you."
    )

    @tool("execute_code", description=description)
    async def execute_code(code: str, state: Annotated[Any, InjectedState] = None) -> str:
        if not code or not code.strip():
            return "Error: execute_code called with empty code."
        # Session attribution for the run's bridged calls (ADR 0103 S3) — the
        # InjectedState lane, not current_session_id() (empty inside tool bodies).
        # Excluded from the model-facing schema; None on a direct ainvoke.
        sid = str(state.get("session_id") or "") if isinstance(state, dict) else ""
        try:
            return await run_code(code, tool_map, timeout=timeout, truncate=truncate, session_id=sid)
        except Exception as exc:
            log.exception("[execute_code] harness failure")
            return f"Error: execute_code harness failed: {type(exc).__name__}: {exc}"

    return execute_code
