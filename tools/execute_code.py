"""execute_code — programmatic tool calling (bd-pe2.6).

Instead of the model emitting one tool call per turn (think → call → read →
think → call …), it writes a single Python script that calls several tools,
loops/filters/composes their results in code, and prints only what matters.
A chain of N tool round-trips collapses into one turn; the model reads just
the script's stdout instead of every intermediate tool payload.

How it runs
-----------
The script executes in a **child Python process** (``python -u <tmpfile>``)
with:

- a **scrubbed environment** — only ``PATH`` + the bridge fds are passed, so
  gateway keys / auth tokens in the parent env are never visible to the script;
- a **hard timeout** (``execute_code_timeout``) after which the process is killed;
- a **tool-RPC bridge**: the script gets a ``tools`` object whose attributes are
  proxies for the allowlisted tools. Calling ``tools.web_search(query=...)``
  serialises the call over a dedicated pipe back to the **parent**, which runs
  the real (async) tool and returns the result. Tools therefore execute with
  the parent's credentials and audit/trace context — the child only orchestrates.

Security posture
----------------
This is **opt-in** (``execute_code.enabled``) and runs model-authored code.
Subprocess + env-scrub + timeout is *isolation, not a true sandbox*: the child
can still touch the filesystem and network as the server user. Enable it only
for trusted-model output or inside a hardened container (seccomp / read-only FS
/ network policy). The ``execute_code`` tool never exposes itself, so scripts
can't recurse into more code execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile

log = logging.getLogger(__name__)

# Prelude prepended to the user's script in the child process. Sets up the
# `tools` proxy object that bridges calls back to the parent over fds named in
# the environment. Kept dependency-free (stdlib only).
_RUNNER_PRELUDE = r'''
import os as _os, sys as _sys, json as _json

_REQ = _os.fdopen(int(_os.environ["EC_REQ_FD"]), "w")   # child -> parent
_RESP = _os.fdopen(int(_os.environ["EC_RESP_FD"]), "r") # parent -> child
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


async def _service_rpc(req_reader: asyncio.StreamReader, resp_writer, tool_map: dict):
    """Read RPC requests from the child, invoke real tools, write back results."""
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
            try:
                result = await tool.ainvoke(args)
                resp = {"id": rid, "ok": True, "result": result if isinstance(result, str) else str(result)}
            except Exception as exc:
                resp = {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            resp_writer.write((json.dumps(resp) + "\n").encode())
            await resp_writer.drain()
        except Exception:
            return  # child gone


async def _connect_read(fd: int):
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, "rb", 0)
    )
    return reader, transport


async def _connect_write(fd: int):
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(fd, "wb", 0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return writer, transport


async def run_code(code: str, tool_map: dict, *, timeout: float = 30.0, truncate: int = 6000) -> str:
    """Run ``code`` in a child process with a tool-RPC bridge; return its stdout."""
    path = _build_runner_file(code)
    # Pipes: child writes requests on req_w; parent writes responses on resp_w.
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()

    proc = None
    req_transport = resp_transport = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUNBUFFERED": "1",
                "EC_REQ_FD": str(req_w),
                "EC_RESP_FD": str(resp_r),
            },
            pass_fds=(req_w, resp_r),
        )
        # Parent doesn't use the child ends; closing req_w lets the parent's
        # reader see EOF when the child exits.
        os.close(req_w); req_w = -1
        os.close(resp_r); resp_r = -1

        req_reader, req_transport = await _connect_read(req_r); req_r = -1
        resp_writer, resp_transport = await _connect_write(resp_w); resp_w = -1

        service = asyncio.ensure_future(_service_rpc(req_reader, resp_writer, tool_map))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: execute_code timed out after {timeout}s (process killed)."
        finally:
            service.cancel()
            try:
                await service
            except asyncio.CancelledError:
                pass  # expected — we just cancelled the service task
            except Exception:  # noqa: BLE001 — teardown failure must not mask the result
                pass

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
        for t in (req_transport, resp_transport):
            if t is not None:
                t.close()
        for fd in (req_w, resp_r, req_r, resp_w):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            os.unlink(path)
        except OSError:
            pass


def build_execute_code_tool(all_tools: list, *, config):
    """Build the ``execute_code`` LangChain tool over an allowlist of tools.

    ``all_tools`` is the agent's full toolset; ``execute_code`` itself is never
    exposed to the script (no recursion). ``config.execute_code_tools`` empty
    means expose all other tools.
    """
    from langchain_core.tools import tool

    allow = set(config.execute_code_tools or [])
    tool_map = {
        t.name: t for t in all_tools
        if t.name != "execute_code" and (not allow or t.name in allow)
    }
    available = ", ".join(sorted(tool_map)) or "(none)"
    timeout = config.execute_code_timeout
    truncate = config.execute_code_output_truncate

    description = (
        "Run a Python script that calls tools programmatically; returns its stdout.\n\n"
        "Use this to collapse a multi-step tool chain into one turn: write a "
        "script that calls several tools, loops/filters/combines their results, "
        "and print() only the final answer. You read just the stdout, not every "
        "intermediate tool payload.\n\n"
        "Inside the script, call tools via the injected `tools` object, e.g.:\n"
        "    results = [tools.web_search(query=q) for q in queries]\n"
        "    print('\\n\\n'.join(results)[:2000])\n\n"
        f"Every tool returns a string. Available tools: {available}\n\n"
        f"The script runs in an isolated subprocess with a {timeout:.0f}s timeout "
        "and a scrubbed environment (no credentials). Only stdout is returned; "
        "write your result with print(). Exceptions and a non-zero exit are "
        "reported back to you."
    )

    @tool("execute_code", description=description)
    async def execute_code(code: str) -> str:
        if not code or not code.strip():
            return "Error: execute_code called with empty code."
        try:
            return await run_code(code, tool_map, timeout=timeout, truncate=truncate)
        except Exception as exc:
            log.exception("[execute_code] harness failure")
            return f"Error: execute_code harness failed: {type(exc).__name__}: {exc}"

    return execute_code
