"""Per-call policy binding for execute_code session kernels (ADR 0134 S2, D3).

Real child processes (like tests/test_execute_code_kernel.py): the kernel outlives
the call that started it, so these pin that every bridged call is checked against the
call running RIGHT NOW. A call opens an active-call slot on entry and clears it on exit
(success, timeout, script exception or cancel); a bridged call that arrives while no
call is active — a thread a previous script left behind — is refused and never runs the
tool. The timeout test blocks the child with ``time.sleep(99)``, a never-returns stub
the timeout kills (≥60s, so the policy gate reads it as a stub, not a real wait).
"""

import asyncio
import json

import pytest
import pytest_asyncio
from langchain_core.tools import tool

from infra import paths
from plugins.execute_code import kernel

pytestmark = pytest.mark.platform_sensitive


def _pid_alive(pid: int | None) -> bool:
    return pid is not None and paths.pid_alive(pid)


@pytest_asyncio.fixture(autouse=True)
async def _no_leaked_kernels():
    """Every test leaves no kernel process alive (ADR 0134 S1a acceptance)."""
    yield
    pids = [k.pid for k in kernel._pool.kernels.values()]
    await kernel.stop_all()
    assert kernel._pool.kernels == {}
    for pid in pids:
        assert not _pid_alive(pid), f"kernel pid {pid} survived teardown"


@pytest.fixture
def audit_probe(tmp_path, monkeypatch):
    """Point the bridged-call audit logger at a scratch file and return its path."""
    from observability.audit import AuditLogger

    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(path))
    return path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _run(code, sid, *, tool_map=None, timeout=30.0, gate=None, fence=None, fresh=False):
    return await kernel.run_in_kernel(
        code, tool_map or {}, timeout=timeout, truncate=6000,
        session_id=sid, gate=gate, fence=fence, fresh=fresh,
    )


@tool
async def echo_tool(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


_TOOL_MAP = {"echo_tool": echo_tool}


@pytest.mark.asyncio
async def test_leftover_thread_between_calls_is_refused(audit_probe, monkeypatch):
    # A thread call 1 leaves running makes a bridged call AFTER call 1 returns, when
    # no execute_code call is active: it is refused outright and the tool never runs.
    sid = "leftover"
    park_entered, park_release, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late_calls: list[str] = []

    @tool
    async def park_tool() -> str:
        """bg parks here (dispatched during call 1); released after call 1 returns."""
        park_entered.set()
        await park_release.wait()
        return "released"

    @tool
    async def confirm_tool() -> str:
        """main waits here until park_tool is parked, so park_tool binds to call 1
        before call 1's exec_result lands — not a race against it."""
        await park_entered.wait()
        return "ok"

    @tool
    async def late_echo(text: str) -> str:
        """The tool the leftover thread tries to call after call 1 returns."""
        late_calls.append(text)
        return text.upper()

    real_record = kernel._record_bridged_call

    def _spy(session_id, name, args, result, duration_ms, success):
        real_record(session_id, name, args, result, duration_ms, success)
        if name == "late_echo" and not success:
            refused.set()

    monkeypatch.setattr(kernel, "_record_bridged_call", _spy)

    tmap = {"park_tool": park_tool, "confirm_tool": confirm_tool, "late_echo": late_echo}
    call1 = (
        "import threading\n"
        "bg_done = threading.Event()\n"
        "box = {}\n"
        "def _bg():\n"
        "    try:\n"
        "        tools.park_tool()\n"
        "        box['late'] = tools.late_echo(text='late')\n"
        "    except Exception as _e:\n"
        "        box['err'] = str(_e)\n"
        "    finally:\n"
        "        bg_done.set()\n"
        "threading.Thread(target=_bg, daemon=True).start()\n"
        "print(tools.confirm_tool())\n"
    )
    assert "ok" in await _run(call1, sid, tool_map=tmap)

    k = kernel._pool.kernels[sid]
    assert k._active is None  # the slot is cleared on exit — no call is active now

    park_release.set()  # bg wakes and calls late_echo with no call active
    await asyncio.wait_for(refused.wait(), 10)
    assert late_calls == []  # the tool was never invoked

    out2 = await _run("bg_done.wait()\nimport json\nprint(json.dumps(box))", sid, timeout=10)
    assert "no execute_code call is active" in out2  # the thread got the refusal

    row = {r["tool"]: r for r in _rows(audit_probe)}["ptc:late_echo"]
    assert row["success"] is False
    assert row["session_id"] == sid  # the refusal row carries the kernel's session
    assert "no execute_code call is active" in row["result_summary"]


@pytest.mark.asyncio
async def test_call2_fence_governs_not_call1(audit_probe):
    # call 1 runs echo_tool with no fence; call 2 runs under a fence that excludes it.
    # call 2's bridged call is checked against call 2's fence, never call 1's.
    sid = "fenced"
    assert "A" in await _run("print(tools.echo_tool(text='a'))", sid, tool_map=_TOOL_MAP)
    code = "try:\n    tools.echo_tool(text='b')\nexcept Exception as e:\n    print('err:', e)"
    out2 = await _run(code, sid, tool_map=_TOOL_MAP, fence=frozenset({"execute_code", "web_search"}))
    assert "Blocked by policy" in out2
    assert "outside this turn's tool allowlist" in out2
    # Each kernel bridged call's audit row carries that call's session_id (ADR 0103 S3).
    rows = [r for r in _rows(audit_probe) if r["tool"] == "ptc:echo_tool"]
    assert [r["success"] for r in rows] == [True, False]
    assert all(r["session_id"] == sid for r in rows)


@pytest.mark.asyncio
async def test_denylisted_tool_refused_in_kernel():
    # An enforcement-gate denylist refuses a bridged call inside a kernel, same text
    # the direct path carries — the bridge is no softer than a model-issued call.
    sid = "denied"
    gate = lambda name: f"Tool '{name}' is disabled by policy." if name == "echo_tool" else None  # noqa: E731
    code = "try:\n    tools.echo_tool(text='x')\nexcept Exception as e:\n    print('err:', e)"
    out = await _run(code, sid, tool_map=_TOOL_MAP, gate=gate)
    assert "Blocked by policy" in out and "disabled by policy" in out


@pytest.mark.asyncio
async def test_result_does_not_leak_into_the_next_call():
    # S1a clears `result` before each exec; confirm one run's value never leaks forward.
    sid = "result"
    assert "first" in await _run("result = {'first': 1}", sid)
    out2 = await _run("print('second call')", sid)
    assert "second call" in out2
    assert "--- result ---" not in out2
    assert "first" not in out2


@pytest.mark.asyncio
async def test_active_slot_cleared_after_timeout():
    sid = "to"
    k, _ = await kernel._pool.get_or_start(sid, interpreter=kernel._resolve_child_interpreter())
    with pytest.raises(kernel._KernelTimeout):
        await k.run("import time\ntime.sleep(99)", tool_map={}, timeout=0.5, session_id=sid, gate=None, fence=None)
    assert k._active is None


@pytest.mark.asyncio
async def test_active_slot_cleared_after_script_exception():
    sid = "exc"
    k, _ = await kernel._pool.get_or_start(sid, interpreter=kernel._resolve_child_interpreter())
    out = await k.run("raise ValueError('boom')", tool_map={}, timeout=30, session_id=sid, gate=None, fence=None)
    assert "Error" in out  # the script raised — a normal (non-raising) return path
    assert k._active is None


@pytest.mark.asyncio
async def test_active_slot_cleared_after_cancel():
    sid = "cancel"
    k, _ = await kernel._pool.get_or_start(sid, interpreter=kernel._resolve_child_interpreter())
    entered, release = asyncio.Event(), asyncio.Event()

    @tool
    async def park() -> str:
        """Parks the in-flight exec so the test can cancel run() mid-call."""
        entered.set()
        await release.wait()
        return "x"

    task = asyncio.create_task(
        k.run("tools.park()", tool_map={"park": park}, timeout=30, session_id=sid, gate=None, fence=None)
    )
    await asyncio.wait_for(entered.wait(), 10)  # the exec is in flight: the slot is open
    assert k._active is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert k._active is None  # cancel runs run()'s finally, which clears the slot
    release.set()  # let the parked dispatch unwind before teardown
