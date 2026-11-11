"""Native workflow isolation: inspect provider inputs, not construction arguments.

Repo guidance is supplied by the registered worker's system prompt. This probe
keeps that policy path real; it does not claim automatic discovery of repo files.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Annotated

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

import graph.agent as agent
import plugins.workflows as wf
from graph.config import LangGraphConfig
from graph.fence_scope import fence_scope
from graph.message_blocks import text_of
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from plugins.workflows.run_state import WorkflowRunStore

pytestmark = pytest.mark.platform_sensitive

PARENT_ONLY = "PARENT_PRIVATE_DISCUSSION_91c772"
DIFF = "diff --git a/review.py b/review.py\n-allow = True\n+allow = checked\n"
FINDINGS = "Prior finding R1: preserve the authorization check.\nEvidence: review.py:12"
REPO_GUIDANCE = "Repository policy: inspect authorization boundaries in every changed handler."
INSTRUCTIONS = "Review instructions: cite file and line for each finding; never modify files."


class ProbeModel(GenericFakeChatModel):
    calls: list = Field(default_factory=list)
    bindings: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bindings.append(sorted(t.name for t in tools))
        return self

    def _generate(self, messages, *args, **kwargs):
        self.calls.append(deepcopy(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture
async def native_probe(monkeypatch, tmp_path):
    @tool
    def read_evidence() -> str:
        """Read review evidence."""
        return "Repository evidence from the child tool."

    @tool
    def read_other() -> str:
        """Read other material."""
        return "Other material."

    @tool
    def write_changes() -> str:
        """Modify the repository."""
        pytest.fail("The review child must not execute a write")

    config = LangGraphConfig(filesystem_enabled=False)
    monkeypatch.setattr(wf.sdk.STATE, "graph_config", config)
    monkeypatch.setattr(wf.sdk.STATE, "plugin_tools", [read_evidence, read_other, write_changes])
    monkeypatch.setattr(wf.sdk.STATE, "mcp_tools", [])
    monkeypatch.setattr(wf.sdk.STATE, "plugin_late_tool_factories", [])
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **kw: [])
    worker = SubagentConfig(
        name="isolation-reviewer", description="Review supplied evidence",
        system_prompt=REPO_GUIDANCE + "\n" + INSTRUCTIONS,
        tools=["read_evidence", "read_other"], inherit_read_tools=False, max_turns=3,
    )
    monkeypatch.setitem(SUBAGENT_REGISTRY, worker.name, worker)
    child = ProbeModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "read_evidence", "args": {}, "id": "read-1"}]),
        AIMessage(content="Review complete."),
    ]))
    monkeypatch.setattr(agent, "create_llm", lambda *a, **kw: child)
    recipe = {
        "name": "isolation", "inputs": [{"name": "diff", "required": True},
                                         {"name": "findings", "required": True}],
        "steps": [{"id": "review", "subagent": worker.name,
                   "prompt": "PR diff:\n{{ inputs.diff }}\nPrior findings:\n{{ inputs.findings }}"}],
    }
    store = WorkflowRunStore(tmp_path)
    results = []

    @tool
    async def run_review(state: Annotated[dict, InjectedState]) -> str:
        """Run the review workflow with explicit review inputs."""
        # Prove the sentinel really exists in the parent tool's live turn state.
        assert PARENT_ONLY in str(state["messages"])
        result = await wf._execute(SimpleNamespace(get=lambda name: recipe), "isolation",
                                   {"diff": DIFF, "findings": FINDINGS}, run_store=store)
        results.append(result)
        return result["output"]

    parent = ProbeModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "run_review", "args": {}, "id": "workflow-1"}]),
        AIMessage(content="Parent finished."),
    ]))
    graph = create_agent(parent, tools=[run_review])

    async def run(fence=()):
        with fence_scope(list(fence)):
            await graph.ainvoke({"messages": [HumanMessage(content=PARENT_ONLY)]})
        assert len(results) == 1
        result = results[0]
        assert result["output"] == "Review complete."
        assert store.load(result["run_id"])["review_context"] == result["review_context"]
        return child, result

    return run


async def test_parent_history_is_absent_but_explicit_review_inputs_survive(native_probe):
    child, result = await native_probe()
    assert len(child.calls) == 2  # initial request AND request after a real tool execution
    expected = f"PR diff:\n{DIFF}\nPrior findings:\n{FINDINGS}"
    for messages in child.calls:
        assert PARENT_ONLY not in str(messages)
        assert [text_of(m) for m in messages if isinstance(m, HumanMessage)] == [expected]
    assert not any(isinstance(m, ToolMessage) for m in child.calls[0])
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "read-1" for m in child.calls[1])
    context = result["review_context"]["steps"]["review"]
    assert context["status"] == "observed"
    assert context["conversation"] == "fresh"
    assert context["parent_history"] == context["inputs"]["history"] == "not_supplied"
    assert context["inputs"]["prompt"]["text"] == expected
    assert context["inputs"]["prompt"]["omitted_characters"] == 0
    assert context["run_id"] == result["run_id"]
    assert context["step_id"] == "review"
    assert context["execution_id"] == result["tool_receipts"]["execution_id"]
    receipt = result["tool_receipts"]["steps"]["review"]
    assert receipt["capture_supported"] and receipt["capture_status"] == "captured"
    assert receipt["dropped_count"] == 0
    assert receipt["pending_count"] == receipt["partial_count"] == 0
    assert [entry["name"] for entry in receipt["entries"]] == ["read_evidence"]


@pytest.mark.parametrize("fenced", [False, True])
async def test_required_guidance_and_effective_tool_restrictions_reach_model(native_probe, fenced):
    fence = ["read_evidence"] if fenced else []
    child, result = await native_probe(fence)
    context = result["review_context"]["steps"]["review"]
    instructions = context["instruction_sources"]
    assert instructions["status"] == "observed"
    for messages in child.calls:
        system = [text_of(m) for m in messages if isinstance(m, SystemMessage)]
        assert system == [REPO_GUIDANCE + "\n" + INSTRUCTIONS]
        assert instructions["system_prompt"]["text"] == system[0]
    # Unknown external discovery stays unknown; configured policy is observed.
    assert instructions["external_sources"] == "unknown"
    restrictions = context["tool_restrictions"]
    assert restrictions["status"] == "observed"
    assert restrictions["bound_tools"] == ["read_evidence", "read_other"]
    assert restrictions["inherited_fence"] == fence
    expected = fence or restrictions["bound_tools"]
    assert child.bindings == [expected, expected]
    assert all("write_changes" not in names and "run_review" not in names for names in child.bindings)
