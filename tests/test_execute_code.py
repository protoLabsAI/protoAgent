"""Tests for programmatic tool calling (execute_code) — bd-pe2.6.

These run real child processes (the Docker CI image has python), exercising
the subprocess + fd-based tool-RPC bridge end to end with fake tools.
"""

import json
import sys
from pathlib import Path

import pytest
from langchain_core.tools import tool

from graph.plugins.registry import PluginRegistry
from plugins.execute_code import register
from plugins.execute_code.engine import build_execute_code_tool, run_code

pytestmark = pytest.mark.platform_sensitive


@tool
async def echo_tool(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


@tool
async def boom_tool() -> str:
    """Always raises."""
    raise ValueError("kaboom")


_TOOL_MAP = {"echo_tool": echo_tool, "boom_tool": boom_tool}


async def _invoke_tool(ec, code, *, tool_call_id="ec-call", **args):
    """Invoke a built ``execute_code`` tool the production way — a full ToolCall, so
    its ``InjectedToolCallId`` (the owning call's id) is populated — and return the
    text body. Direct dictionary calls are also supported, without a parent id."""
    msg = await ec.ainvoke(
        {"name": "execute_code", "args": {"code": code, **args}, "id": tool_call_id, "type": "tool_call"}
    )
    return msg.content


@pytest.mark.asyncio
async def test_plain_stdout_no_tools():
    out = await run_code("print('hello world')", {})
    assert out == "hello world"


@pytest.mark.asyncio
@pytest.mark.parametrize("expression, expected", [
    ('{"a": 1, "b": [1, 2]}', {"a": 1, "b": [1, 2]}),
    ('["caf\\u00e9", 2]', ["café", 2]),
    ('{"values": {1}}', {"values": "{1}"}),
    ("None", None),
])
async def test_structured_result(expression, expected):
    out = await run_code(f"result = {expression}", {})
    assert json.loads(out) == expected
    assert "\\u00e9" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("code, needle", [
    # A non-str dict key (tuple) is unreachable by default= — which only converts
    # VALUES — so json.dumps raises TypeError. Pre-fix this failed the run.
    ('result = {(1, 2): "x"}', "(1, 2)"),
    # A circular reference makes json.dumps raise ValueError.
    ('result = {}\nresult["self"] = result', "..."),
])
async def test_unserialisable_result_falls_back_without_failing(code, needle):
    out = await run_code(code, {})
    # The run still succeeds (exit 0); the result is handed back as a best-effort
    # string rather than crashing the script where a plain run exited 0.
    assert not out.startswith("Error:")
    decoded = json.loads(out)
    assert isinstance(decoded, str)
    assert needle in decoded


@pytest.mark.asyncio
async def test_stdout_and_structured_result():
    out = await run_code('print("gathered", end="")\nresult = {"a": 1}', {})
    assert out == 'gathered\n--- result ---\n{"a": 1}'


@pytest.mark.asyncio
@pytest.mark.parametrize("ending, error", [
    ("raise ValueError('bad script')", "Error: script exited with code 1."),
    ("import sys; sys.exit(2)", "Error: script exited with code 2."),
    ("import sys; sys.exit(0)", None),
    ("", None),
])
async def test_result_cleanup_and_early_exit(ending, error):
    code = 'import os\nprint(os.environ["EC_RESULT_PATH"])\nresult = {"a": 1}\n' + ending
    out = await run_code(code, {})
    if ending:
        assert "--- result ---" not in out
        assert '{"a": 1}' not in out
    if error:
        assert out.startswith(error)
        path_text = out.split("--- stdout before failure ---\n", 1)[1]
    elif ending:
        path_text = out
    else:
        path_text, result_text = out.split("\n--- result ---\n")
        assert json.loads(result_text) == {"a": 1}
    assert not Path(path_text).parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "print('gathered')\n"])
async def test_oversized_result_spills(tmp_path, monkeypatch, prefix):
    monkeypatch.setattr("graph.sdk.plugin_store", lambda **kwargs: tmp_path)
    full = ('gathered\n--- result ---\n' if prefix else '') + json.dumps({"a": "x" * 100})
    out = await run_code(prefix + 'result = {"a": "x" * 100}', {}, truncate=20)
    assert out.startswith(full[:20])
    assert f"of {len(full)} chars" in out
    spill, = tmp_path.glob("ec-*.txt")
    assert str(spill) in out
    assert spill.read_text(encoding="utf-8") == full


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
    out = await _invoke_tool(ec, "print(tools.echo_tool(text='hi'))")
    assert out == "HI"


@pytest.mark.asyncio
async def test_built_tool_rejects_empty():
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    out = await _invoke_tool(ec, "  ")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_default_bridge_memory_and_knowledge_search(newline):
    @tool
    async def memory_search(query: str) -> str:
        """Search saved memories."""
        return f"memory: {query}"

    @tool
    async def knowledge_search(query: str) -> str:
        """Search reference documents."""
        return f"knowledge: {query}"

    @tool
    async def memory_recall(query: str) -> str:
        """Retired search name must not be exposed by default."""
        raise AssertionError("retired tool called")

    ec = build_execute_code_tool([memory_search, knowledge_search, memory_recall])
    for name in ("memory_search", "knowledge_search"):
        assert f"tools.{name}(query" in ec.description
    assert "memory_recall" not in ec.description
    out = await _invoke_tool(ec, (
        "import sys\n"
        f"sys.stdout.reconfigure(newline={newline!r})\n"
        "print(tools.memory_search(query='preferences'))\n"
        "print(tools.knowledge_search(query='reference'))\n"
        "try:\n"
        "    tools.memory_recall(query='legacy')\n"
        "except RuntimeError as exc:\n"
        "    print(exc)"
    ))
    assert out.splitlines()[:2] == ["memory: preferences", "knowledge: reference"]
    assert "not available" in out


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


def test_a_changed_setting_applies_on_the_first_reload(monkeypatch):
    # A reload builds the new graph before the host swaps in the new config, so the host's
    # "live" plugin section is still the OLD one while the factory runs. The factory is
    # handed the NEW graph config and must read its own section from that — otherwise
    # turning session_state on took two reloads.
    from types import SimpleNamespace

    from runtime.state import STATE

    old = SimpleNamespace(plugin_config={"execute_code": {"session_state": False}})
    new = SimpleNamespace(plugin_config={"execute_code": {"session_state": True}})
    monkeypatch.setattr(STATE, "graph_config", old)
    reg = PluginRegistry("execute_code", Path("."), config={"session_state": False})
    register(reg)
    factory = reg.late_tool_factories[0]
    assert "State PERSISTS across calls" in factory([echo_tool], new).description
    # No section on the passed config (or no config at all): the live read still applies.
    assert "no state persists between runs" in factory([echo_tool], None).description
    assert "no state persists between runs" in factory([echo_tool], SimpleNamespace(plugin_config={})).description


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

    code = "print(tools.echo_tool(text='ok'))\ntry:\n    tools.boom_tool()\nexcept RuntimeError:\n    print('caught')\n"
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


# --- execute_code audit parent-link (one-shot) -------------------------------


def _audit_rows(path):
    import json as _json

    return [_json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_ptc_rows_carry_parent_call_id_and_script_sha256(tmp_path, monkeypatch):
    """Two bridged calls from one execute_code call: both ptc: rows carry the
    model's tool-call id as ``parent_call_id`` and the script's sha256 as
    ``script_sha256`` — so an operator can tell which script made each bridged call."""
    import hashlib

    from observability.audit import AuditLogger

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "audit.jsonl"))
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    code = "print(tools.echo_tool(text='a'))\nprint(tools.echo_tool(text='b'))\n"
    out = await _invoke_tool(ec, code, tool_call_id="call-abc", state={"session_id": "sess-1"})
    assert out.replace("\r\n", "\n") == "A\nB"

    sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    rows = _audit_rows(tmp_path / "audit.jsonl")
    echoes = [r for r in rows if r["tool"] == "ptc:echo_tool"]
    assert len(echoes) == 2
    for r in echoes:
        assert r["parent_call_id"] == "call-abc"
        assert r["script_sha256"] == sha
    # the ptc:script row links the same call and keeps the exact script
    script_row = next(r for r in rows if r["tool"] == "ptc:script")
    assert script_row["parent_call_id"] == "call-abc"
    assert script_row["script_sha256"] == sha
    assert script_row["code"] == code


@pytest.mark.asyncio
async def test_two_calls_in_one_session_get_distinct_parent_call_ids(tmp_path, monkeypatch):
    """The session id alone can't separate two runs in one session; the per-call
    ``parent_call_id`` now does — the two calls' ptc: rows carry different ids."""
    from observability.audit import AuditLogger

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "audit.jsonl"))
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    await _invoke_tool(ec, "print(tools.echo_tool(text='a'))", tool_call_id="call-one", state={"session_id": "sess-X"})
    await _invoke_tool(ec, "print(tools.echo_tool(text='b'))", tool_call_id="call-two", state={"session_id": "sess-X"})

    rows = _audit_rows(tmp_path / "audit.jsonl")
    echoes = [r for r in rows if r["tool"] == "ptc:echo_tool"]
    assert {r["parent_call_id"] for r in echoes} == {"call-one", "call-two"}
    assert {r["session_id"] for r in echoes} == {"sess-X"}  # same session, distinguishable runs


@pytest.mark.asyncio
async def test_script_row_keeps_full_script_and_caps_at_64kb(tmp_path, monkeypatch):
    """The script lives in the ptc:script row's ``code`` in full (not clipped at the
    500-char _sanitize_args limit), and a script over 64 KB is cut to 64 KB and flagged."""
    from observability.audit import AuditLogger
    from plugins.execute_code.engine import _SCRIPT_CODE_CAP

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "long.jsonl"))
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    long_code = "x = 0  # " + ("pad " * 200) + "\nprint('done')\n"
    assert len(long_code) > 500
    await _invoke_tool(ec, long_code, tool_call_id="call-long", state={"session_id": "sess-L"})
    script_row = next(r for r in _audit_rows(tmp_path / "long.jsonl") if r["tool"] == "ptc:script")
    assert script_row["code"] == long_code  # full script, not clipped at 500 chars
    assert "code_truncated" not in script_row

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "big.jsonl"))
    huge = "pad = '" + ("z" * 70000) + "'\n"
    assert len(huge) > _SCRIPT_CODE_CAP
    await _invoke_tool(ec, huge, tool_call_id="call-huge", state={"session_id": "sess-H"})
    big_row = next(r for r in _audit_rows(tmp_path / "big.jsonl") if r["tool"] == "ptc:script")
    assert len(big_row["code"]) == _SCRIPT_CODE_CAP == 64 * 1024
    assert big_row["code"] == huge[:_SCRIPT_CODE_CAP]
    assert big_row["code_truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("with_state", [False, True])
async def test_ainvoke_without_tool_call_id_records_rows_without_parent(tmp_path, monkeypatch, with_state):
    """A direct invoke with no tool-call id (empty) still runs and records ptc: rows —
    they just carry no ``parent_call_id`` link (the id is treated as empty)."""
    from observability.audit import AuditLogger

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "audit.jsonl"))
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    code = "print(tools.echo_tool(text='hi'))"
    args = {"code": code}
    if with_state:
        args["state"] = {"session_id": "sess-noid"}
    out = await ec.ainvoke(args)
    assert isinstance(out, str)
    assert out.replace("\r\n", "\n") == "HI"
    assert set(ec.tool_call_schema.model_json_schema()["properties"]) == {"code", "fresh"}

    rows = _audit_rows(tmp_path / "audit.jsonl")
    echo = next(r for r in rows if r["tool"] == "ptc:echo_tool")
    assert "parent_call_id" not in echo  # no id → no link, but the row is still written
    script_row = next(r for r in rows if r["tool"] == "ptc:script")
    assert "parent_call_id" not in script_row
    assert script_row["code"] == code  # the script is still kept, just unlinked


@pytest.mark.asyncio
async def test_script_and_bridged_rows_redact_credentials(tmp_path, monkeypatch):
    """A one-shot run bypasses the graph's AuditMiddleware — the one place model-path
    tool rows are redacted — so this path applies the same redact() itself: a credential
    hardcoded in the script (the ptc:script `code`) or handed to a bridged tool (the ptc:
    row's args) is scrubbed, never persisted in the audit log in plaintext."""
    from observability.audit import AuditLogger

    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(tmp_path / "audit.jsonl"))
    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    secret = "sk-" + "a" * 40  # an OpenAI-shaped token the redactor matches by shape
    code = f'API_KEY = "{secret}"\nprint(tools.echo_tool(text="{secret}"))\n'
    await _invoke_tool(ec, code, tool_call_id="call-sec", state={"session_id": "sess-sec"})

    rows = _audit_rows(tmp_path / "audit.jsonl")
    script_row = next(r for r in rows if r["tool"] == "ptc:script")
    assert secret not in script_row["code"]  # the script is redacted, not stored raw
    assert "[REDACTED]" in script_row["code"]
    echo = next(r for r in rows if r["tool"] == "ptc:echo_tool")
    assert echo["args"] == {"text": "[REDACTED]"}  # the bridged call's args are redacted too
    assert secret not in json.dumps(echo["args"])


def test_audit_log_without_extra_keeps_row_shape(tmp_path):
    """``AuditLogger.log`` without ``extra`` (or ``extra=None``) writes the same row
    shape as before; a populated ``extra`` merges its keys in verbatim."""
    import json as _json

    from observability.audit import AuditLogger

    a = AuditLogger(tmp_path / "a.jsonl")
    a.log(session_id="s", tool="t", args={"a": 1}, result_summary="ok", duration_ms=5, success=True)
    a.log(session_id="s", tool="t", args={"a": 1}, result_summary="ok", duration_ms=5, success=True, extra=None)
    r0, r1 = [_json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert set(r0) == set(r1)  # extra=None writes the identical row shape
    assert {"ts", "session_id", "tool", "args", "result_summary", "duration_ms", "success"} <= set(r0)

    a.log(
        session_id="s", tool="ptc:script", args={}, result_summary="", duration_ms=0, success=True,
        extra={"parent_call_id": "c9", "code": "z" * 600},
    )
    r2 = _json.loads((tmp_path / "a.jsonl").read_text().splitlines()[2])
    assert r2["parent_call_id"] == "c9"
    assert len(r2["code"]) == 600  # merged verbatim, not clipped by _sanitize_args


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
    out = await run_code("print(tools.echo_tool(text='ok'))", _TOOL_MAP, fence=frozenset({"execute_code", "echo_tool"}))
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


@pytest.mark.asyncio
async def test_parallel_calls_finish_in_time_of_slowest_call():
    import asyncio
    import time

    delay_seconds = 0.5

    @tool
    async def slow_echo(n: int) -> str:
        """Return a distinct result after simulated I/O."""
        # Real delay is intentional: this acceptance test measures parallel I/O.
        await asyncio.sleep(delay_seconds)
        return str(n)

    started = time.monotonic()
    out = await run_code(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "with ThreadPoolExecutor(8) as pool:\n"
        "    print(list(pool.map(lambda n: tools.slow_echo(n=n), range(8))))\n",
        {"slow_echo": slow_echo},
    )
    assert time.monotonic() - started < 2.0
    assert out == str([str(n) for n in range(8)])


@pytest.mark.asyncio
async def test_parallel_calls_respect_concurrency_cap():
    import asyncio

    from plugins.execute_code.engine import _MAX_PARALLEL_CALLS

    active = peak = 0
    full = asyncio.Event()

    @tool
    async def counted(n: int) -> str:
        """Track concurrent calls, releasing once the first batch fills."""
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == _MAX_PARALLEL_CALLS:
            full.set()
        try:
            await full.wait()
            await asyncio.sleep(0.01)
            return str(n)
        finally:
            active -= 1

    count = _MAX_PARALLEL_CALLS * 3
    out = await run_code(
        "from concurrent.futures import ThreadPoolExecutor\n"
        f"with ThreadPoolExecutor({count}) as pool:\n"
        f"    print(list(pool.map(lambda n: tools.counted(n=n), range({count}))))\n",
        {"counted": counted}, timeout=5,
    )
    assert out == str([str(n) for n in range(count)])
    assert peak == _MAX_PARALLEL_CALLS
    assert active == 0


@pytest.mark.asyncio
async def test_parallel_denial_is_audited_while_allowed_calls_succeed(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from observability.audit import AuditLogger
    from plugins.execute_code.engine import _build_enforcement_gate

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("observability.audit.audit_logger", AuditLogger(audit_path))
    gate = _build_enforcement_gate(SimpleNamespace(
        enforcement_enabled=True, enforcement_disallowed_tools=["boom_tool"],
        enforcement_rate_limits=None,
    ))
    out = await run_code(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def one(n):\n"
        "    try:\n"
        "        return tools.boom_tool() if n == 1 else tools.echo_tool(text=str(n))\n"
        "    except RuntimeError as e:\n"
        "        return str(e)\n"
        "with ThreadPoolExecutor(3) as pool:\n"
        "    print(list(pool.map(one, range(3))))\n",
        _TOOL_MAP, gate=gate, session_id="parallel-policy",
    )
    assert "'0'" in out and "'2'" in out and "Blocked by policy" in out
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(rows) == 3
    assert all(row["session_id"] == "parallel-policy" for row in rows)
    denied = next(row for row in rows if row["tool"] == "ptc:boom_tool")
    assert denied["success"] is False
    assert "disabled by policy" in denied["result_summary"]
    assert all(row["success"] for row in rows if row["tool"] == "ptc:echo_tool")


@pytest.mark.asyncio
async def test_timeout_cancels_parallel_tool_tasks():
    import asyncio

    from plugins.execute_code.engine import _MAX_PARALLEL_CALLS

    running = set()
    started = 0

    @tool
    async def blocked() -> str:
        """Stay in flight until the run cancels the call."""
        nonlocal started
        task = asyncio.current_task()
        running.add(task)
        started += 1
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            running.remove(task)

    out = await run_code(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "with ThreadPoolExecutor(16) as pool:\n"
        "    print(list(pool.map(lambda n: tools.blocked(), range(16))))\n",
        {"blocked": blocked}, timeout=1,
    )
    assert "timed out" in out
    assert started == _MAX_PARALLEL_CALLS
    assert not running


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["null_id", "closed"])
async def test_bridge_failure_releases_every_waiter(failure):
    import asyncio

    @tool
    async def blocked() -> str:
        """Keep callers waiting until their bridge fails."""
        await asyncio.Event().wait()
        return "unreachable"

    action = (
        "_REQ.write('not json\\n'); _REQ.flush()"
        if failure == "null_id" else "_sock.shutdown(_socket.SHUT_RDWR)"
    )
    code = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "import time\n"
        "def one(n):\n"
        "    try:\n"
        "        return tools.blocked()\n"
        "    except RuntimeError as e:\n"
        "        return str(e)\n"
        "with ThreadPoolExecutor(4) as pool:\n"
        "    futures = [pool.submit(one, n) for n in range(4)]\n"
        "    while True:\n"
        "        with _LOCK:\n"
        "            if len(_PENDING) == 4:\n"
        f"                {action}\n"
        "                break\n"
        "        time.sleep(0.001)\n"
        "    print([f.result() for f in futures])\n"
    )
    if failure == "null_id":
        code += "print(tools.echo_tool(text='recovered'))\n"
    out = await run_code(code, {**_TOOL_MAP, "blocked": blocked}, timeout=5)
    message = "malformed tool-bridge request" if failure == "null_id" else "tool bridge closed before responding"
    assert out.count(message) == 4
    if failure == "null_id":
        assert out.endswith("RECOVERED")


# ── ptc_call live events: one start + one end custom event per bridged call ──


def _capturing_sink():
    events: list[dict] = []

    async def sink(payload):
        events.append(payload)

    return events, sink


_TWO_CALLS = "print(tools.echo_tool(text='a'), tools.echo_tool(text='b'))"


@pytest.mark.asyncio
async def test_bridged_calls_emit_ptc_call_events_under_the_execute_code_run():
    # The production lane: the tool body captures its run config, so each bridged
    # call surfaces as a `ptc_call` custom event on the caller's callbacks.
    from langchain_core.callbacks import AsyncCallbackHandler

    events: list[dict] = []

    class _Capture(AsyncCallbackHandler):
        async def on_custom_event(self, name, data, **kwargs):
            assert name == "ptc_call"
            events.append(data)

    ec = build_execute_code_tool([echo_tool], tools=["echo_tool"])
    msg = await ec.ainvoke(
        {"name": "execute_code", "args": {"code": _TWO_CALLS}, "id": "call-live", "type": "tool_call"},
        config={"callbacks": [_Capture()]},
    )
    assert "A B" in msg.content
    assert [e["phase"] for e in events] == ["start", "end", "start", "end"]
    assert all(e["parent_call_id"] == "call-live" and e["name"] == "echo_tool" for e in events)
    assert [e["id"] for e in events] == ["call-live:ptc:1", "call-live:ptc:1", "call-live:ptc:2", "call-live:ptc:2"]
    first_start, first_end, second_start, second_end = events
    assert set(first_start) == {"phase", "id", "name", "args", "parent_call_id"}
    assert (first_start["args"], second_start["args"]) == ({"text": "a"}, {"text": "b"})
    assert set(first_end) == {"phase", "id", "name", "output", "output_chars", "error", "duration_ms", "parent_call_id"}
    assert (first_end["output"], first_end["output_chars"], first_end["error"]) == ("A", 1, False)
    assert second_end["output"] == "B" and isinstance(second_end["duration_ms"], int)


@pytest.mark.asyncio
async def test_ptc_call_end_caps_output_and_flags_a_failed_tool():
    events, sink = _capturing_sink()
    code = "tools.echo_tool(text='x' * 5000)\ntry:\n    tools.boom_tool()\nexcept RuntimeError:\n    pass\nprint('ok')"
    assert "ok" in await run_code(code, _TOOL_MAP, parent_call_id="c1", ptc_sink=sink)
    big, boom = [e for e in events if e["phase"] == "end"]
    assert (len(big["output"]), big["output_chars"], big["error"]) == (2000, 5000, False)
    assert boom["error"] is True and "kaboom" in boom["output"]


@pytest.mark.asyncio
async def test_policy_denied_bridged_call_emits_start_and_error_end():
    events, sink = _capturing_sink()
    gate = lambda name: f"Tool '{name}' is disabled by policy."  # noqa: E731
    code = "try:\n    tools.echo_tool(text='x')\nexcept RuntimeError as e:\n    print('err:', e)"
    out = await run_code(code, _TOOL_MAP, gate=gate, parent_call_id="call-deny", ptc_sink=sink)
    assert "Blocked by policy" in out
    start, end = events
    assert (start["phase"], start["id"], start["args"]) == ("start", "call-deny:ptc:1", {"text": "x"})
    assert (end["phase"], end["id"], end["error"]) == ("end", "call-deny:ptc:1", True)
    assert end["output"].startswith("Blocked by policy:") and "disabled by policy" in end["output"]
    assert end["parent_call_id"] == start["parent_call_id"] == "call-deny"


@pytest.mark.asyncio
async def test_no_sink_emits_nothing_and_a_raising_sink_keeps_the_result(monkeypatch):
    from plugins.execute_code import engine

    emitted: list[dict] = []

    async def _spy(self, payload):
        emitted.append(payload)

    monkeypatch.setattr(engine._PtcEvents, "_emit", _spy)
    baseline = await run_code(_TWO_CALLS, _TOOL_MAP, parent_call_id="c1")  # no sink
    events, sink = _capturing_sink()
    assert await run_code(_TWO_CALLS, _TOOL_MAP, ptc_sink=sink) == baseline  # a sink but no parent call id
    assert baseline == "A B" and emitted == [] and events == []
    monkeypatch.undo()

    attempts: list[dict] = []

    async def _raising(payload):
        attempts.append(payload)
        raise RuntimeError("stream is gone")

    assert await run_code(_TWO_CALLS, _TOOL_MAP, parent_call_id="c1", ptc_sink=_raising) == baseline
    assert len(attempts) == 4  # every emit was tried; none reached the bridged call


@pytest.mark.asyncio
async def test_ptc_call_events_stop_after_200_bridged_calls_but_calls_still_run():
    events, sink = _capturing_sink()
    code = "print(sum(len(tools.echo_tool(text='x')) for _ in range(205)))"
    assert await run_code(code, _TOOL_MAP, parent_call_id="c1", ptc_sink=sink, timeout=60) == "205"
    assert [e["phase"] for e in events].count("start") == 200
    assert [e["phase"] for e in events].count("end") == 200
    assert len({e["id"] for e in events}) == 200 and events[-1]["id"] == "c1:ptc:200"
