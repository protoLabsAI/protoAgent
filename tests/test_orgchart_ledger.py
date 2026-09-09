"""orgChart overlays REAL delegation volume on the capability graph.

The chart has always drawn who *can* delegate to whom. The ledger says who actually did,
how often, and how it went — which is the difference between a wiring diagram and a
record, and the point of the whole arc.
"""

from __future__ import annotations

import pytest

from plugins.orgchart import topology


@pytest.fixture(autouse=True)
def _no_host(monkeypatch):
    """No ledger store by default — the overlay must degrade, never fail shut."""
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "ledger_store", None, raising=False)


def _nodes():
    return {
        "http://self": {"id": "http://self", "name": "gina", "kind": "self", "up": True},
        "acp:http://self#coder": {"id": "acp:http://self#coder", "name": "coder", "kind": "acp", "up": True},
        "http://peer": {"id": "http://peer", "name": "hermes", "kind": "agent", "up": True},
    }


def _rows(**over):
    base = dict(
        to_kind="acp", to_name="coder", dispatches=7, ok=5, failed=2, cancelled=0,
        cost_usd=1.5, priced=5, duration_ms=1000, last_at="2026-09-09T02:00:00+00:00",
    )
    base.update(over)
    return base


def test_work_lands_on_the_delegate_it_belongs_to():
    nodes, edges = _nodes(), []
    topology._apply_work(nodes, edges, "http://self", [_rows()])

    work = nodes["acp:http://self#coder"]["work"]
    assert work["dispatches"] == 7
    assert (work["ok"], work["failed"]) == (5, 2)


def test_a_configured_but_unused_delegate_carries_no_work():
    # The distinction a capability graph cannot draw: wired up versus actually used.
    nodes, edges = _nodes(), []
    topology._apply_work(nodes, edges, "http://self", [_rows()])

    assert "work" not in nodes["http://peer"]


def test_an_a2a_peer_matches_despite_the_graph_calling_it_an_agent():
    # The ledger records a peer as `a2a`; the chart draws it with kind "agent".
    nodes, edges = _nodes(), []
    topology._apply_work(nodes, edges, "http://self", [_rows(to_kind="a2a", to_name="hermes")])

    assert nodes["http://peer"]["work"]["dispatches"] == 7


def test_subagents_appear_as_nodes_because_the_chart_never_had_them():
    # For an agent whose delegation is all in-process, this is the entire picture — the
    # chart would otherwise show none of its actual work.
    nodes, edges = _nodes(), []
    topology._apply_work(
        nodes, edges, "http://self", [_rows(to_kind="subagent", to_name="researcher")]
    )

    node = nodes["subagent:researcher"]
    assert node["kind"] == "subagent"
    assert node["work"]["dispatches"] == 7
    assert {"from": "http://self", "to": "subagent:researcher", "kind": "task"} in edges


def test_a_subagent_has_no_liveness_because_it_runs_inside_its_owner():
    # `up: None` is "unknown/not applicable", which the view already renders differently
    # from a confident red. An in-process subagent has nothing to probe.
    nodes, edges = _nodes(), []
    topology._apply_work(nodes, edges, "http://self", [_rows(to_kind="subagent", to_name="r")])

    assert nodes["subagent:r"]["up"] is None


def test_subagent_nodes_are_not_duplicated_across_rebuilds():
    nodes, edges = _nodes(), []
    rows = [_rows(to_kind="subagent", to_name="researcher")]
    topology._apply_work(nodes, edges, "http://self", rows)
    topology._apply_work(nodes, edges, "http://self", rows)

    assert len([n for n in nodes if n.startswith("subagent:")]) == 1


def test_priced_travels_with_cost_so_partial_sums_are_not_read_as_totals():
    nodes, edges = _nodes(), []
    topology._apply_work(nodes, edges, "http://self", [_rows(dispatches=10, cost_usd=1.0, priced=2)])

    work = nodes["acp:http://self#coder"]["work"]
    assert (work["cost_usd"], work["priced"], work["dispatches"]) == (1.0, 2, 10)


def test_the_overlay_is_a_no_op_without_a_ledger():
    # An older host has no ledger store; the chart must still draw what it always drew.
    assert topology._ledger_edges() == []


def test_malformed_ledger_rows_are_skipped_rather_than_drawn():
    nodes, edges = _nodes(), []
    topology._apply_work(
        nodes, edges, "http://self",
        [{"to_kind": "", "to_name": "x"}, {"to_kind": "subagent", "to_name": ""}],
    )

    assert not any(n.startswith("subagent:") for n in nodes)
