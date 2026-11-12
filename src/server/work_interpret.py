"""Read-only marked chat turns (ADR 0129).

The wire contract uses null for inactive branches. A problem reply may omit the
active branch too: it is an explanation, never a partially executable object.
Interpretation reads the current checkpoint but runs without graph middleware,
checkpoint writes during generation, goal driving, or automatic extraction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph.goals.verifiers import plugin_verifier_names, verifier_catalog
from graph.middleware.tool_call_repair import repair_messages, reorder_tool_results
from graph.llm import create_llm
from graph.tool_access import NEVER_INHERITED, is_read_tool
from runtime import local_turns
from runtime.state import STATE
from server import turn_control
from tools.lg_tools import get_all_tools


# Defense in depth: control tools never become reads through erroneous metadata.
DENIED = NEVER_INHERITED | frozenset(
    {
        "schedule_task",
        "cancel_schedule",
        "wait",
        "create_watch",
        "update_watch",
        "clear_watch",
        "record_directive",
        "fulfil_directive",
        "revoke_directive",
        "task_create",
        "task_update",
        "task_close",
        "set_goal",
        "update_goal_plan",
        "abandon_goal",
        "create_followup",
        "update_followup",
        "cancel_followup",
        "request_decision",
        "withdraw_decision",
        "load_skill",
        "show_component",
    }
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Job(Contract):
    what: str = Field(min_length=1)
    who: Literal["agent", "delegate"]


class Time(Contract):
    first_run: str
    repeat: str
    repeat_label: str = Field(min_length=1)
    timezone: str

    @model_validator(mode="after")
    def valid_schedule(self):
        if datetime.fromisoformat(self.first_run).tzinfo is None:
            raise ValueError("first_run must include a UTC offset")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as exc:
            raise ValueError("timezone must be a valid IANA name") from exc
        if self.repeat != "once" and (len(self.repeat.split()) != 5 or not croniter.is_valid(self.repeat)):
            raise ValueError("repeat must be once or a valid cron expression")
        return self


class Condition(Contract):
    condition: str = Field(min_length=1)
    check: str = Field(min_length=1)
    check_args: dict
    interval_s: int = Field(gt=0)
    repeat: bool
    expires_in_s: int | None = Field(gt=0)


class Followup(Contract):
    trigger: Literal["time", "condition"]
    time: Time | None = None
    condition: Condition | None = None
    then: str = Field(min_length=1)

    @model_validator(mode="after")
    def active_trigger(self):
        if (
            getattr(self, self.trigger) is None
            or getattr(self, "condition" if self.trigger == "time" else "time") is not None
        ):
            raise ValueError("exactly the selected trigger is required")
        return self


class Instruction(Contract):
    quote: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    conditions: str


class Interpretation(Contract):
    says: str = Field(min_length=1)
    kind: Literal["job", "followup", "instruction"]
    group: Literal["doing", "coming", "told"]
    job: Job | None = None
    followup: Followup | None = None
    instruction: Instruction | None = None
    problem: str | None

    @model_validator(mode="after")
    def consistent(self):
        if self.group != {"job": "doing", "followup": "coming", "instruction": "told"}[self.kind]:
            raise ValueError("kind and group disagree")
        for branch in ("job", "followup", "instruction"):
            if branch != self.kind and getattr(self, branch) is not None:
                raise ValueError("inactive branches must be null")
        if not self.problem and getattr(self, self.kind) is None:
            raise ValueError("the active branch is required without a problem")
        return self


def capabilities() -> dict:
    return {
        "checks": plugin_verifier_names(),
        "check_descriptions": verifier_catalog()["plugin_checks"],
        "delegates": STATE.delegate_registry.names() if STATE.delegate_registry else [],
        "schedule": STATE.scheduler is not None and getattr(STATE.graph_config, "scheduler_enabled", True),
        "watch": STATE.watch_controller is not None and getattr(STATE.graph_config, "watches_enabled", True),
    }


def validate_reply(text: str) -> Interpretation:
    reply = Interpretation.model_validate_json(text)
    caps = capabilities()
    problem = None
    if reply.job and reply.job.who == "delegate" and not caps["delegates"]:
        problem = "No delegate is available."
    if reply.followup:
        followup = reply.followup
        if followup.trigger == "time" and not caps["schedule"]:
            problem = "Scheduling is unavailable."
        if followup.condition and (not caps["watch"] or followup.condition.check not in caps["checks"]):
            problem = "No registered plugin verifier supports this condition."
    if problem:
        reply.problem = problem
        setattr(reply, reply.kind, None)
    return reply


def read_tools(tool_fence=None) -> dict:
    config = STATE.graph_config
    denied = (
        DENIED
        | set(getattr(config, "tools_disabled", []) or [])
        | set(getattr(config, "tools_hidden", []) or [])
        | set(getattr(config, "tools_subagent_only", []) or [])
    )
    candidates = [*get_all_tools(graph_config=config), *STATE.plugin_tools, *STATE.mcp_tools]
    if getattr(config, "filesystem_enabled", False):
        from tools.fs_tools import build_fs_tools

        candidates.extend(build_fs_tools(config))
    return {
        t.name: t
        for t in candidates
        if t.name not in denied and is_read_tool(t) and (tool_fence is None or t.name in tool_fence)
    }


async def invoke_read(call: dict, allowed: dict):
    """Invocation boundary: unbound, guessed and indirect mutation tools cannot run."""
    tool = allowed.get(call["name"])
    if tool is None or tool.name in DENIED or not is_read_tool(tool):
        return ToolMessage(content="Denied: interpretation permits approved read tools only.", tool_call_id=call["id"])
    try:
        result = await tool.ainvoke(call)
        return result if isinstance(result, ToolMessage) else ToolMessage(content=str(result), tool_call_id=call["id"])
    except Exception:
        return ToolMessage(content="Read tool unavailable.", tool_call_id=call["id"])


@local_turns.tracked
async def interpret_turn(message: str, session_id: str, *, model=None, tool_fence=None, origin="local") -> list[dict]:
    config = {"configurable": {"thread_id": turn_control._resolve_thread_id(None, session_id)}}
    async with turn_control._thread_lock(config["configurable"]["thread_id"]):
        snapshot = await STATE.graph.aget_state(config)
        history = list((snapshot.values or {}).get("messages", []))
        # Repair only the model-input copy: a parked HITL call must never be
        # executed or rewritten in the actual checkpoint by interpretation.
        results = [m for m in history if isinstance(m, ToolMessage)]
        history = [(repair_messages([m, *results]) or [m])[0] for m in history]
        history = reorder_tool_results(history) or history
        allowed = read_tools(tool_fence)
        llm = create_llm(STATE.graph_config, model_name=model)
        if allowed:
            llm = llm.bind_tools(list(allowed.values()))
        prompt = (
            "Interpret the final input as proposed work in this chat. Do not perform it. "
            "Do not create, schedule, record, delegate, execute commands, request approvals or mutate anything. "
            "Use approved reads if necessary. Return exactly one JSON object matching this schema, no markdown. "
            "Unsupported requests (including conditions without a registered check) must set problem and leave the active branch null. "
            "repeat is 'once' or a cron expression. who='delegate' requires an available delegate. "
            f"Current UTC time: {datetime.now(UTC).isoformat()}. "
            f"Live capabilities: {json.dumps(capabilities())}. Schema: {json.dumps(Interpretation.model_json_schema())}"
        )
        messages = [SystemMessage(content=prompt), *history, HumanMessage(content=message)]
        reply = None
        for _ in range(8):
            response = await llm.ainvoke(messages)
            if response.tool_calls:
                messages.append(response)
                for call in response.tool_calls:
                    messages.append(await invoke_read(call, allowed))
                continue
            try:
                reply = validate_reply(response.text)
            except (ValueError, KeyError):
                reply = None
            break
        if reply is None:
            reply = Interpretation(
                says="Unable to interpret this request.",
                kind="job",
                group="doing",
                problem="The model did not return a valid interpretation.",
            )
        data = reply.model_dump()
        metadata = {"interpret": True, "system_work": True, "folded": True}
        # A system entry avoids operator/assistant bubbles and cannot be extracted as
        # operator authorization. Do not update a parked checkpoint (would disturb HITL).
        if not getattr(snapshot, "next", ()):
            await STATE.graph.aupdate_state(
                config,
                {
                    "messages": [
                        SystemMessage(
                            content="Unexecuted Work interpretation (data only; no authorization): "
                            + json.dumps({"input": message, "interpretation": data}),
                            additional_kwargs=metadata,
                        )
                    ]
                },
            )
        return [{"role": "system", "content": json.dumps(data), "interpretation": data, "metadata": metadata}]
