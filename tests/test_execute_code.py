"""Tests for programmatic tool calling (execute_code) — bd-pe2.6.

These run real child processes (the Docker CI image has python), exercising
the subprocess + fd-based tool-RPC bridge end to end with fake tools.
"""

import sys
from pathlib import Path

import pytest
from langchain_core.tools import tool

from graph.plugins.registry import PluginRegistry
from plugins.execute_code import register
from plugins.execute_code.engine import build_execute_code_tool, run_code


@tool
async def echo_tool(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


@tool
async def boom_tool() -> str:
    """Always raises."""
    raise ValueError("kaboom")


_TOOL_MAP = {"echo_tool": echo_tool, "boom_tool": boom_tool}


@pytest.mark.asyncio
async def test_plain_stdout_no_tools():
    out = await run_code("print('hello world')", {})
    assert out == "hello world"


@pytest.mark.asyncio
async def test_tool_bridge_roundtrip():
    out = await run_code("print(tools.echo_tool(text='abc'))", _TOOL_MAP)
    assert out == "ABC"


@pytest.mark.asyncio
async def test_tool_bridge_loop_collapses_chain():
    code = "vals = [tools.echo_tool(text=w) for w in ['a', 'b', 'c']]\nprint('-'.join(vals))"
    out = await run_code(code, _TOOL_MAP)
    assert out == "A-B-C"


@pytest.mark.asyncio
async def test_tool_error_propagates_to_script():
    # The tool raises; the proxy surfaces it as a RuntimeError the script can see.
    code = "try:\n    tools.boom_tool()\nexcept Exception as e:\n    print('caught:', e)"
    out = await run_code(code, _TOOL_MAP)
    assert "caught:" in out and "kaboom" in out


@pytest.mark.asyncio
async def test_unknown_tool_reported():
    code = "try:\n    tools.nope()\nexcept Exception as e:\n    print('err:', e)"
    out = await run_code(code, _TOOL_MAP)
    assert "not available" in out


@pytest.mark.asyncio
async def test_script_exception_reports_nonzero_exit():
    out = await run_code("raise ValueError('bad script')", {})
    assert "exited with code" in out
    assert "bad script" in out


@pytest.mark.asyncio
async def test_timeout_kills_process():
    out = await run_code("import time; time.sleep(5)", {}, timeout=0.5)
    assert "timed out" in out


@pytest.mark.asyncio
async def test_env_is_scrubbed(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    out = await run_code("import os; print(os.environ.get('SECRET_TOKEN', 'ABSENT'))", {})
    assert out == "ABSENT"


@pytest.mark.asyncio
async def test_threaded_tool_calls_never_cross_responses():
    """Regression (live friction log): ``tools.*`` from a ThreadPoolExecutor
    returned file N's content for file N+1, and some calls died with
    JSONDecodeError at char ~8190. The child's bridge shared one socket
    makefile pair with no lock — concurrent writes interleaved (the text-mode
    buffer flushes in ~8K chunks, splitting JSON frames the parent then
    dropped) and concurrent readlines raced for each other's replies.

    Real child process, real socket bridge: 8 threads x 32 calls, each request
    and each reply >16KB and distinct, and every result must be its own."""
    from langchain_core.tools import tool as _tool

    @_tool
    async def blob_tool(n: int, pad: str) -> str:
        """Return a large payload unique to ``n`` (echoing the request's pad length)."""
        return f"<{n}:{len(pad)}>" + chr(65 + n % 26) * 20_000 + f"</{n}>"

    code = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def one(n):\n"
        "    try:\n"
        "        body = tools.blob_tool(n=n, pad=str(n) * 20000)\n"
        "    except Exception as e:\n"
        "        return n, 'EXC ' + type(e).__name__ + ': ' + str(e)[:120]\n"
        "    want = f'<{n}:{len(str(n) * 20000)}>' + chr(65 + n % 26) * 20000 + f'</{n}>'\n"
        "    return n, 'ok' if body == want else 'MISMATCH got ' + body[:24]\n"
        "with ThreadPoolExecutor(8) as ex:\n"
        "    results = list(ex.map(one, range(32)))\n"
        "bad = [(n, r) for n, r in results if r != 'ok']\n"
        "print('bad:', len(bad), bad[:3])\n"
    )
    out = await run_code(code, {"blob_tool": blob_tool}, timeout=30.0)
    assert out.replace("\r\n", "\n") == "bad: 0 []"


@pytest.mark.asyncio
async def test_malformed_frame_is_answered_not_dropped():
    """A frame the parent can't parse gets an error reply (by id when its head
    still carries one) — dropping it parked the waiting caller until the hard
    timeout. The script drives the bridge's raw stream to send the bad frames."""
    code = (
        "import json\n"
        '_REQ.write(\'{"id": 99, "tool": oops\\n\'); _REQ.flush()\n'
        "r1 = json.loads(_RESP.readline())\n"
        "_REQ.write('not json at all\\n'); _REQ.flush()\n"
        "r2 = json.loads(_RESP.readline())\n"
        "print(r1['id'], r1['ok'], r2['id'], r2['ok'], 'malformed' in r1['error'])\n"
        "print(tools.echo_tool(text='still works'))\n"
    )
    out = await run_code(code, _TOOL_MAP, timeout=10.0)
    assert out.replace("\r\n", "\n") == "99 False None False True\nSTILL WORKS"


@pytest.mark.asyncio
async def test_output_truncation(tmp_path, monkeypatch):
    # Overflow now spills the FULL stdout to a scratch file and returns the head
    # plus a marker naming it (#3701); redirect the store to tmp so the test is
    # self-contained. The spill-failure fallback path (legacy marker) and the full
    # spill contract are covered in tests/test_execute_code_spill_3701.py.
    def _fake_store(subdir="", *, plugin_id):
        d = tmp_path / plugin_id / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("graph.sdk.plugin_store", _fake_store)
    out = await run_code("print('x' * 100)", {}, truncate=20)
    assert out.startswith("x" * 20)
    assert "of 100 chars" in out
    spill = list((tmp_path / "execute_code" / "spill").glob("ec-*.txt"))
    assert len(spill) == 1 and spill[0].read_text() == "x" * 100


# --- tool-build wiring ------------------------------------------------------


def test_build_excludes_self_and_respects_allowlist():
    # include a decoy + a self-named tool to prove filtering
    ec = build_execute_code_tool([echo_tool, boom_tool], tools=["echo_tool"])
    assert ec.name == "execute_code"
    # allowlist limited to echo_tool; the docstring lists available tools
    assert "echo_tool" in ec.description
    assert "boom_tool" not in ec.description


@pytest.mark.asyncio
async def test_built_tool_runs():
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    out = await ec.ainvoke({"code": "print(tools.echo_tool(text='hi'))"})
    assert out == "HI"


@pytest.mark.asyncio
async def test_built_tool_rejects_empty():
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    out = await ec.ainvoke({"code": "  "})
    assert "empty code" in out


# --- the bridge allowlist is a posture (ADR 0103 D3, #2807) -------------------


def test_default_bridge_set_is_curated_not_everything():
    """No configured allowlist used to expose EVERY registered tool. The default
    is now the curated read-mostly set — an unknown fake tool stays unbridged."""
    from langchain_core.tools import tool as _tool

    @_tool
    async def read_file(project: str, path: str) -> str:
        """Fake core read tool (name matches the curated set)."""
        return "content"

    ec = build_execute_code_tool([echo_tool, boom_tool, read_file])
    assert "read_file" in ec.description  # curated-set member: bridged
    assert "echo_tool" not in ec.description  # arbitrary tool: NOT bridged by default
    assert "boom_tool" not in ec.description


def test_hitl_and_delegation_are_never_bridgeable():
    """ADR 0103 D3/D6: an interrupt can't park a subprocess, and delegation from
    model-written code is out of scope — even an EXPLICIT config entry can't
    bridge them (structural denial, not a policy default)."""
    from langchain_core.tools import tool as _tool

    @_tool
    async def ask_human(question: str) -> str:
        """Fake HITL tool."""
        return "?"

    @_tool
    async def task(subagent_type: str, prompt: str) -> str:
        """Fake delegation tool."""
        return "done"

    ec = build_execute_code_tool([ask_human, task, echo_tool], tools=["ask_human", "task", "echo_tool"])
    assert "ask_human" not in ec.description
    assert "task" not in ec.description
    assert "echo_tool" in ec.description  # the explicit list otherwise works


# --- plugin wiring ----------------------------------------------------------


def test_plugin_register_wires_a_late_factory_that_builds_the_tool():
    reg = PluginRegistry(
        "execute_code", Path("."), config={"timeout": 5, "output_truncate": 100, "tools": ["echo_tool"]}
    )
    register(reg)
    assert len(reg.late_tool_factories) == 1
    ec = reg.late_tool_factories[0]([echo_tool, boom_tool], None)
    assert ec.name == "execute_code"
    # allowlist from the plugin's config section is applied
    assert "echo_tool" in ec.description and "boom_tool" not in ec.description


# --- additive extra_tools bridge (#3701) -------------------------------------


@tool
async def read_file(project: str, path: str) -> str:
    """Fake core read tool (name matches the curated default set)."""
    return "content"


@tool
async def github_search_issues(query: str) -> str:
    """Fake plugin read tool the operator wants to bridge additively."""
    return "issues"


def _factory_tool(config, all_tools):
    """Register with a config and build the tool through the late factory."""
    reg = PluginRegistry("execute_code", Path("."), config=config)
    register(reg)
    return reg.late_tool_factories[0](all_tools, None)


def test_extra_tools_adds_on_top_of_curated_default():
    # tools empty → curated default applies; extra_tools adds one plugin read
    # tool WITHOUT the operator re-listing every core tool.
    ec = _factory_tool(
        {"tools": [], "extra_tools": ["github_search_issues"]},
        [read_file, github_search_issues, echo_tool],
    )
    assert "read_file" in ec.description  # curated default member still bridged
    assert "github_search_issues" in ec.description  # additively bridged
    assert "echo_tool" not in ec.description  # not in default, not in extra


def test_extra_tools_unions_with_explicit_list():
    # An explicit `tools` list REPLACES the default; extra_tools unions on top.
    ec = _factory_tool(
        {"tools": ["echo_tool"], "extra_tools": ["github_search_issues"]},
        [echo_tool, github_search_issues, boom_tool, read_file],
    )
    assert "echo_tool" in ec.description
    assert "github_search_issues" in ec.description
    assert "read_file" not in ec.description  # explicit list replaced the default
    assert "boom_tool" not in ec.description


def test_extra_tools_unknown_name_is_ignored_not_raised():
    # A typo / unregistered name must be dropped silently (logged), never raise.
    ec = _factory_tool(
        {"tools": [], "extra_tools": ["not_a_real_tool"]},
        [read_file],
    )
    assert "read_file" in ec.description  # default set intact
    assert "not_a_real_tool" not in ec.description


def test_execute_code_cannot_bridge_itself_via_extra_tools():
    # Even with a decoy tool literally named execute_code present, it must not be
    # bridged through extra_tools (no recursion / self-escalation).
    @tool("execute_code")
    async def execute_code_decoy(code: str) -> str:
        """Decoy tool sharing the reserved name."""
        return ""

    ec = _factory_tool(
        {"tools": [], "extra_tools": ["execute_code"]},
        [read_file, execute_code_decoy],
    )
    assert "read_file" in ec.description
    assert "tools.execute_code(" not in ec.description


def test_no_extra_tools_preserves_existing_behavior():
    # extra_tools empty → the effective list is exactly the explicit `tools`
    # config (None passes through so the engine applies its curated default).
    from plugins.execute_code import _effective_tools

    assert _effective_tools(None, [], [read_file]) is None
    assert _effective_tools(["echo_tool"], [], [echo_tool]) == ["echo_tool"]


def test_plugin_registers_in_frozen_build(monkeypatch):
    # ADR 0094: the frozen desktop build registers the tool (the child runs on the
    # managed CPython) — the old silent skip presented a toggle that did nothing (#2137).
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    reg = PluginRegistry("execute_code", Path("."), config={})
    register(reg)
    assert len(reg.late_tool_factories) == 1


# --- child interpreter resolution (ADR 0094) ---------------------------------


def test_child_interpreter_is_own_python_when_not_frozen():
    from plugins.execute_code.engine import _resolve_child_interpreter

    assert _resolve_child_interpreter() == sys.executable


def test_child_interpreter_uses_managed_python_when_frozen(monkeypatch, tmp_path):
    from plugins.execute_code.engine import _resolve_child_interpreter

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe = tmp_path / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("infra.python_runtime.managed_python_exe", lambda: exe)
    assert _resolve_child_interpreter() == str(exe)


@pytest.mark.asyncio
async def test_frozen_without_runtime_answers_with_install_path(monkeypatch):
    # No managed runtime provisioned: the tool must answer with the actionable install
    # path — a RESULT, not a raise, so the thread never strands a dangling tool_call.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("infra.python_runtime.managed_python_exe", lambda: None)
    out = await run_code("print('hi')", {})
    assert out.startswith("Error:")
    assert "install-python" in out and "Settings" in out


# --- the spike measurement (ADR 0103 S1, #2807) -------------------------------


@pytest.mark.asyncio
async def test_ptc_collapse_mechanics_ten_reads_one_round():
    """The number the spike exists to produce, in its deterministic half: a
    10-read investigation through the bridge is ONE tool round whose
    model-visible output is a small fraction of the bytes the loop equivalent
    would have pushed through the context.

    Loop mode: 10 tool rounds, each ~5KB result entering history and riding
    every later call (cache-read after #2777, pruned at pressure after #2782 —
    but present). PTC mode: the intermediate 50KB stays in the subprocess;
    the model reads only the printed digest."""
    from langchain_core.tools import tool as _tool

    payload = "x" * 5_000

    calls = {"n": 0}

    @_tool
    async def read_file(project: str, path: str) -> str:
        """Fake 5KB read."""
        calls["n"] += 1
        return f"[{path}]\n{payload}"

    code = (
        "sizes = {}\n"
        "for i in range(10):\n"
        "    body = tools.read_file(project='demo', path=f'f{i}.txt')\n"
        "    sizes[f'f{i}.txt'] = len(body)\n"
        "print('files:', len(sizes), 'total bytes:', sum(sizes.values()))\n"
    )
    out = await run_code(code, {"read_file": read_file})

    assert calls["n"] == 10  # ten real tool executions happened…
    assert out == "files: 10 total bytes: 50090"  # …behind ONE model-visible result
    intermediate_bytes = 10 * (len(payload) + len("[f0.txt]\n"))
    # The model-visible output is <0.1% of what loop mode would have re-sent —
    # the collapse the ADR gates on, measured rather than asserted by vibes.
    assert len(out) < intermediate_bytes * 0.001


# --- S2: schema-visible signatures (ADR 0103, #2807) --------------------------


def test_description_carries_call_signatures_not_bare_names():
    """The proxy is name-only on the wire; the model's contract is the tool
    DESCRIPTION — it now shows real signatures (params + defaults + first line
    of each tool's description) so kwargs are written, not guessed."""
    from langchain_core.tools import tool as _tool

    @_tool
    async def read_file(project: str, path: str, offset: int = 1) -> str:
        """Read a text file inside a managed project (relative path)."""
        return ""

    ec = build_execute_code_tool([read_file], tools=["read_file"])
    assert "tools.read_file(project, path, offset=1)" in ec.description
    assert "Read a text file inside a managed project" in ec.description


def test_signature_lines_are_budgeted():
    """A wide explicit allowlist must not balloon the schema — past the cap the
    rest list by name with the calling shape noted."""
    from langchain_core.tools import tool as _tool

    def _mk(i):
        @_tool(f"t{i:02d}")
        async def _t(x: str) -> str:
            """A tiny tool."""
            return x

        return _t

    many = [_mk(i) for i in range(30)]
    ec = build_execute_code_tool(many, tools=[t.name for t in many])
    assert "(+5 more, same calling shape:" in ec.description


# --- S3: bridged-call observability (ADR 0103, #2807) -------------------------


@pytest.mark.asyncio
async def test_bridged_calls_land_in_audit_and_metrics(tmp_path, monkeypatch):
    """Direct ainvoke bypasses the graph's audit middleware, so a script's tool
    calls were INVISIBLE to the operator. Each bridged call now writes an audit
    row (tool `ptc:<name>` — the via-tag as a greppable prefix) with duration and
    success, attributed to the run's session."""
    import json as _json

    from observability.audit import AuditLogger

    probe = AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr("observability.audit.audit_logger", probe)

    code = (
        "print(tools.echo_tool(text='ok'))\n"
        "try:\n"
        "    tools.boom_tool()\n"
        "except RuntimeError:\n"
        "    print('caught')\n"
    )
    out = await run_code(code, _TOOL_MAP, session_id="sess-ptc-test")
    # The child's stdout is platform-newlined — CRLF on Windows (the native CI
    # lane caught exactly this). Normalize for the assertion; the engine
    # deliberately returns stdout verbatim.
    assert out.replace("\r\n", "\n") == "OK\ncaught"

    rows = [_json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["ptc:echo_tool"]["success"] is True
    assert by_tool["ptc:echo_tool"]["session_id"] == "sess-ptc-test"
    assert by_tool["ptc:echo_tool"]["result_summary"] == "OK"
    assert by_tool["ptc:boom_tool"]["success"] is False
    assert "kaboom" in by_tool["ptc:boom_tool"]["result_summary"]


# --- binding-path parity (ADR 0103 S4, #2807) --------------------------------


@pytest.mark.asyncio
async def test_fence_blocks_bridged_call_outside_allowlist():
    # A fenced background run that allowlists execute_code must not bridge past
    # the fence — same reason text the SubagentFenceMiddleware ToolMessage carries.
    code = "try:\n    tools.echo_tool(text='x')\nexcept Exception as e:\n    print('err:', e)"
    out = await run_code(code, _TOOL_MAP, fence=frozenset({"execute_code", "web_search"}))
    assert "Blocked by policy" in out
    assert "outside this turn's tool allowlist" in out


@pytest.mark.asyncio
async def test_fence_allows_listed_tool():
    out = await run_code(
        "print(tools.echo_tool(text='ok'))", _TOOL_MAP, fence=frozenset({"execute_code", "echo_tool"})
    )
    assert out == "OK"


@pytest.mark.asyncio
async def test_enforcement_gate_denylist_blocks_bridged_call():
    gate = lambda name: f"Tool '{name}' is disabled by policy." if name == "echo_tool" else None  # noqa: E731
    code = "try:\n    tools.echo_tool(text='x')\nexcept Exception as e:\n    print('err:', e)"
    out = await run_code(code, _TOOL_MAP, gate=gate)
    assert "Blocked by policy" in out and "disabled by policy" in out


@pytest.mark.asyncio
async def test_enforcement_rate_limit_applies_within_a_run():
    # The same sliding-window limits a model-issued call meets (own window,
    # per the documented S4 deviation) — the second bridged call inside one
    # run must hit the limit.
    from plugins.execute_code.engine import _build_enforcement_gate

    class _Cfg:
        enforcement_enabled = True
        enforcement_disallowed_tools = None
        enforcement_rate_limits = {"echo_tool": {"max": 1, "window_seconds": 60}}

    gate = _build_enforcement_gate(_Cfg())
    code = (
        "print(tools.echo_tool(text='a'))\n"
        "try:\n    tools.echo_tool(text='b')\nexcept Exception as e:\n    print('err:', e)"
    )
    out = await run_code(code, _TOOL_MAP, gate=gate)
    assert "A" in out  # first call passed
    assert "Blocked by policy" in out  # second hit the window


def test_enforcement_gate_built_only_when_configured():
    from types import SimpleNamespace

    from plugins.execute_code.engine import _build_enforcement_gate

    assert _build_enforcement_gate(None) is None
    assert (
        _build_enforcement_gate(
            SimpleNamespace(
                enforcement_enabled=False,
                enforcement_disallowed_tools=["echo_tool"],
                enforcement_rate_limits=None,
            )
        )
        is None
    )
    gate = _build_enforcement_gate(
        SimpleNamespace(
            enforcement_enabled=True,
            enforcement_disallowed_tools=["boom_tool"],
            enforcement_rate_limits=None,
        )
    )
    assert gate is not None
    assert gate("boom_tool") and gate("echo_tool") is None
