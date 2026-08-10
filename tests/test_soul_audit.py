"""Persona untooled-action audit (#2276) — detection is pure; the wiring warns and
publishes but never blocks or raises.

The failure this guards is silent by design: a persona commanding an action with no
backing tool produces no error — the model narrates the action as done. So the tests
pin BOTH directions: the flagship misses are found, and the prose that merely *talks
about* tools or reads state never warns (a noisy audit gets ignored, which is the same
as no audit)."""

from types import SimpleNamespace

import graph.config_io as config_io
from graph.soul_audit import audit_untooled_actions

# The bound set of a small but realistic build: core tools only, github.write off.
CORE_TOOLS = ["task", "memory_recall", "knowledge_ingest", "schedule_task", "github_list_issues"]


# ── capability tier ──────────────────────────────────────────────────────────


def test_the_flagship_case_filed_as_issues_without_a_write_tool_is_found():
    """The exact project-manager-preset sentence that shipped the narrated-success bug —
    past tense and all. github_list_issues (read-only) must not count as backing."""
    soul = "Pain points found along the way get filed as issues — contributing them back is part of the job."

    findings = audit_untooled_actions(soul, CORE_TOOLS)

    assert [f["kind"] for f in findings] == ["capability"]
    assert findings[0]["action"] == "file issues"
    assert "filed as issues" in findings[0]["evidence"]


def test_a_registered_write_tool_backs_the_commitment():
    soul = "Pain points get filed as issues."

    assert audit_untooled_actions(soul, [*CORE_TOOLS, "github_create_issue"]) == []


def test_reading_the_queue_is_not_a_commitment_to_file():
    """"Open issues" is how personas describe the READ side; only file/create/raise
    phrasing commits to a write."""
    soul = "Review the open issues each morning and summarize them for the operator."

    assert audit_untooled_actions(soul, CORE_TOOLS) == []


def test_send_email_is_found_and_a_gmail_send_tool_backs_it():
    soul = "Send a weekly summary email to the operator."

    assert [f["action"] for f in audit_untooled_actions(soul, CORE_TOOLS)] == ["send email"]
    assert audit_untooled_actions(soul, [*CORE_TOOLS, "gmail_send_message"]) == []


def test_post_to_slack_and_calendar_commitments():
    soul = "Post standup updates to Slack, and schedule the quarterly planning meetings."

    actions = {f["action"] for f in audit_untooled_actions(soul, CORE_TOOLS)}
    assert actions == {"post to Slack", "manage calendar events"}
    backed = audit_untooled_actions(soul, [*CORE_TOOLS, "slack_post_message", "google_calendar_create_event"])
    assert backed == []


def test_run_shell_commands_is_backed_by_run_command():
    soul = "Run shell commands to verify a fix before reporting it."

    assert [f["action"] for f in audit_untooled_actions(soul, CORE_TOOLS)] == ["run shell commands"]
    assert audit_untooled_actions(soul, [*CORE_TOOLS, "run_command"]) == []


# ── tool-mention tier ────────────────────────────────────────────────────────


def test_an_unbound_verb_led_tool_mention_is_found_and_a_bound_one_is_not():
    soul = "Use `run_command` for cleanups, and memory_recall before answering."

    findings = audit_untooled_actions(soul, CORE_TOOLS)

    assert [(f["kind"], f["action"]) for f in findings] == [("tool_mention", "run_command")]


def test_infrastructure_identifiers_never_warn():
    """Noun-led snake_case is the infra namespace (module names, config keys) — a persona
    mentioning it is describing the world, not commanding an action."""
    soul = "State lives under host_config; the A2A surface is a2a_impl and history is in soul_history."

    assert audit_untooled_actions(soul, CORE_TOOLS) == []


def test_repeated_mentions_dedupe_to_one_finding():
    soul = "Prefer edit_soul for persona fixes. If edit_soul is unavailable, say so."

    findings = audit_untooled_actions(soul, CORE_TOOLS)

    assert [f["action"] for f in findings] == ["edit_soul"]


# ── contract ─────────────────────────────────────────────────────────────────


def test_empty_persona_and_determinism():
    assert audit_untooled_actions("", CORE_TOOLS) == []
    assert audit_untooled_actions("   \n", CORE_TOOLS) == []
    soul = "Send email updates and use run_command."
    assert audit_untooled_actions(soul, CORE_TOOLS) == audit_untooled_actions(soul, CORE_TOOLS)


# ── server wiring (_audit_persona_tools) ─────────────────────────────────────


def _wired(monkeypatch, soul: str, tool_names: list[str]):
    """Run agent_init._audit_persona_tools against a fake graph + captured bus."""
    from server import agent_init

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(agent_init, "_event_bus", SimpleNamespace(publish=lambda t, d: events.append((t, d))))
    monkeypatch.setattr(config_io, "read_soul", lambda: soul)
    monkeypatch.setattr(config_io, "soul_revision", lambda: "cafebabe")
    graph = SimpleNamespace(bound_tools=[SimpleNamespace(name=n) for n in tool_names])
    agent_init._audit_persona_tools(graph, trigger="boot")
    return events


def test_wiring_publishes_one_event_carrying_all_findings(monkeypatch):
    events = _wired(monkeypatch, "Pain points get filed as issues. Send email reports.", CORE_TOOLS)

    assert len(events) == 1
    topic, payload = events[0]
    assert topic == "persona.untooled_action_detected"
    assert payload["trigger"] == "boot"
    assert payload["soul_revision"] == "cafebabe"
    assert payload["count"] == 2
    assert {f["action"] for f in payload["findings"]} == {"file issues", "send email"}


def test_wiring_is_silent_when_the_persona_is_fully_tooled(monkeypatch):
    events = _wired(monkeypatch, "Be helpful and concise.", CORE_TOOLS)

    assert events == []


def test_wiring_skips_a_none_graph_and_swallows_audit_failures(monkeypatch):
    from server import agent_init

    events: list = []
    monkeypatch.setattr(agent_init, "_event_bus", SimpleNamespace(publish=lambda t, d: events.append((t, d))))
    agent_init._audit_persona_tools(None, trigger="boot")  # setup pending — no tools bound

    monkeypatch.setattr(config_io, "read_soul", lambda: (_ for _ in ()).throw(OSError("disk")))
    graph = SimpleNamespace(bound_tools=[SimpleNamespace(name="task")])
    agent_init._audit_persona_tools(graph, trigger="reload")  # must not raise into boot/reload

    assert events == []
