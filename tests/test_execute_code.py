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
async def test_output_truncation():
    out = await run_code("print('x' * 100)", {}, truncate=20)
    assert out.startswith("x" * 20)
    assert "truncated to 20 chars" in out


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
