"""The bundle template reacts to member releases immediately (#2960 S1).

examples/bundles/template/.github/workflows/verify-bundle.yml gains a
`member-released` repository_dispatch trigger so an archetype's `bump` job fires
the moment a member plugin repo publishes a release, instead of waiting for the
weekly cron; examples/bundles/member-release-notify.yml is the member-side
release.yml step that sends the dispatch. These are templates — inert under
examples/ — so the contract they encode is only enforceable here, by parsing.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "bundles" / "template" / ".github" / "workflows" / "verify-bundle.yml"
NOTIFY = ROOT / "examples" / "bundles" / "member-release-notify.yml"


def _triggers(workflow: dict) -> dict:
    # PyYAML reads a bare `on:` key as YAML-1.1 boolean True.
    return workflow.get("on") or workflow.get(True)


def test_member_released_dispatch_triggers_verify_bundle() -> None:
    workflow = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert _triggers(workflow)["repository_dispatch"] == {"types": ["member-released"]}


def test_bump_gate_passes_repository_dispatch_and_skips_pull_request() -> None:
    """`bump` must run on the new dispatch but keep skipping PR-triggered runs.

    The single `!= 'pull_request'` inequality is load-bearing: it admits
    schedule, workflow_dispatch, AND repository_dispatch without enumerating
    them, so the new trigger needs no job-level change. Tightening it to an
    allowlist that omits repository_dispatch would silently undo #2960.
    """
    workflow = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    condition = " ".join(workflow["jobs"]["bump"]["if"].split())
    assert condition == "github.event_name != 'pull_request'"


def test_pr_verify_and_weekly_cron_are_unchanged() -> None:
    """S1 adds the dispatch only — PR-triggered verify and the Monday cron stay
    as they were (the cadence change is S2's slice)."""
    workflow = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    triggers = _triggers(workflow)
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    assert [entry["cron"] for entry in triggers["schedule"]] == ["17 6 * * 1"]


def test_member_notify_step_dispatches_member_released_to_archetypes() -> None:
    """The member-side snippet sends the exact event the template listens for,
    only on runs that actually created a release (`exists == 'false'`)."""
    workflow = yaml.safe_load(NOTIFY.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    notify = next(step for step in steps if step.get("name") == "Notify archetype repos")

    assert notify["if"] == "steps.v.outputs.exists == 'false'"
    assert "gh api repos/$repo/dispatches" in notify["run"]
    assert "event_type=member-released" in notify["run"]
    assert "client_payload[plugin]=${{ github.repository }}" in notify["run"]
    assert "client_payload[tag]=${{ steps.v.outputs.tag }}" in notify["run"]


def test_member_notify_snippet_defaults_to_no_archetypes() -> None:
    """`archetype_repos` defaults to empty on both triggers, so a plugin that is
    in no archetype carries the notify step as a no-op."""
    workflow = yaml.safe_load(NOTIFY.read_text(encoding="utf-8"))
    triggers = _triggers(workflow)
    for trigger in ("workflow_dispatch", "workflow_call"):
        assert triggers[trigger]["inputs"]["archetype_repos"]["default"] == ""
