"""``protoagent fleet friction`` — read every member's ledger through the hub; resolve by id.

The hub client is stubbed at ``deck.hub.connect`` (same seam as test_fleet_cli). What is under
test: which members are read, filtering, the cross-agent "shared" rollup, one member failing
without losing the rest, and that resolve never has a default scope.
"""

from __future__ import annotations

import json

import pytest

from deck import hub as deckhub
from graph.fleet import cli, friction_cli

NOW = "2026-10-16T10:00:00+00:00"
OLD = "2026-09-01T10:00:00+00:00"

ROSTER = [
    {"name": "hub", "label": "hub", "id": "hub", "running": True, "host": True},
    {"name": "alpha", "label": "alpha", "id": "alpha-1", "running": True},
    {"name": "beta", "label": "beta", "id": "beta-2", "running": True},
    {"name": "idle", "label": "idle", "id": "idle-3", "running": False},
    {"name": "ava", "label": "ava", "id": "r-ava", "running": False, "remote": True},
]


def _item(kind, summary, severity="minor", count=1, seen=NOW, detail="", tool=""):
    return {
        "id": friction_id(kind, summary), "kind": kind, "summary": summary, "severity": severity,
        "count": count, "first_seen": seen, "last_seen": seen, "detail": detail, "tool": tool,
    }


def friction_id(kind, summary):
    import hashlib

    return hashlib.sha1(f"{kind}\x00{summary}".encode()).hexdigest()[:8]


SHARED = _item("harness", "read_file truncates at 50k chars", "major", 4, detail="needs from_line")
LEDGERS = {
    "host": [_item("harness", "board_cancel is not idempotent", "minor", 1)],
    "alpha-1": [SHARED, _item("model", "wrong path guessed", "minor", 2), _item("harness", "ancient thing", "major", 1, seen=OLD)],
    "beta-2": [dict(SHARED, count=3), _item("harness", "flaky list_dir", "minor", 9)],
}


class FakeClient:
    url = "http://127.0.0.1:7870"

    def __init__(self, roster=None, ledgers=None, fail=None):
        self.roster = list(ROSTER if roster is None else roster)
        self.ledgers = {k: list(v) for k, v in (ledgers or LEDGERS).items()}
        self.fail = fail or {}
        self.calls = []

    def member_get(self, slug, rel, **params):
        self.calls.append(("get", slug, rel, params))
        if slug in self.fail:
            raise self.fail[slug]
        rows = self.ledgers.get(slug, [])
        if params.get("kind"):
            rows = [r for r in rows if r["kind"] == params["kind"]]
        return {"items": rows, "total": len(rows)}

    def member_post(self, slug, rel, body=None):
        self.calls.append(("post", slug, rel, body))
        rows = self.ledgers.get(slug, [])
        n = sum(1 for r in rows if r["summary"] == body["summary"] and r["kind"] == body["kind"])
        return {"changed": n, "resolved": body["resolved"]}

    def close(self):
        pass


def _live(monkeypatch, client):
    conn = deckhub.Connection(
        client=client, candidate=deckhub.HubCandidate(client.url, "heartbeat"), card={"name": "hub"}, roster=list(client.roster)
    )
    monkeypatch.setattr(deckhub, "connect", lambda **kw: conn)


def _run(argv):
    return cli.run_fleet_cli(argv)


def test_reads_every_running_member_and_names_the_ones_it_skipped(monkeypatch, capsys):
    c = FakeClient()
    _live(monkeypatch, c)
    assert _run(["friction", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out["results"]] == ["hub", "alpha", "beta"]
    assert {s["name"]: s["reason"] for s in out["skipped"]} == {"idle": "stopped", "ava": "unreachable"}
    assert {call[1] for call in c.calls} == {"host", "alpha-1", "beta-2"}  # the hub rides as `host`


def test_a_signal_hit_by_several_agents_is_surfaced_as_shared(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    _run(["friction", "--json"])
    shared = json.loads(capsys.readouterr().out)["shared"]
    assert len(shared) == 1
    assert shared[0]["summary"] == SHARED["summary"]
    assert sorted(shared[0]["agents"]) == ["alpha", "beta"] and shared[0]["count"] == 7


def test_rows_are_ranked_worst_first_per_agent(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    _run(["friction", "--json"])
    alpha = next(r for r in json.loads(capsys.readouterr().out)["results"] if r["name"] == "alpha")
    # severity first, then how often, then recency
    assert [i["summary"] for i in alpha["items"]] == [SHARED["summary"], "ancient thing", "wrong path guessed"]


def test_filters_severity_kind_since_and_agent(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    # an absolute cutoff: a relative `7d` would move with the real clock
    _run(["friction", "--json", "--severity", "major", "--since", "2026-10-01", "--agent", "alpha"])
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out["results"]] == ["alpha"]
    assert [i["summary"] for i in out["results"][0]["items"]] == [SHARED["summary"]]  # 'ancient thing' is too old


def test_since_accepts_windows_and_rejects_nonsense():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert friction_cli.parse_since("7d", now=now).startswith("2026-10-09")
    assert friction_cli.parse_since("12h", now=now).startswith("2026-10-15T12")
    with pytest.raises(ValueError):
        friction_cli.parse_since("lately")


def test_one_member_failing_is_reported_not_fatal(monkeypatch, capsys):
    err = deckhub.HubRequestError(FakeClient.url, 404, "not found")
    _live(monkeypatch, FakeClient(fail={"beta-2": err}))
    assert _run(["friction", "--json"]) == 1  # nonzero: the read was incomplete
    out = json.loads(capsys.readouterr().out)
    beta = next(r for r in out["results"] if r["name"] == "beta")
    assert beta["ok"] is False and "no friction ledger" in beta["error"]
    assert next(r for r in out["results"] if r["name"] == "alpha")["ok"] is True


def test_a_rejected_member_credential_is_not_blamed_on_the_hub(monkeypatch, capsys):
    err = deckhub.MemberUnauthorized(FakeClient.url, "beta-2", "x")
    _live(monkeypatch, FakeClient(fail={"beta-2": err}))
    _run(["friction", "--json"])
    beta = next(r for r in json.loads(capsys.readouterr().out)["results"] if r["name"] == "beta")
    assert "rejected the credential" in beta["error"]


def test_text_output_lists_ids_and_the_shared_section(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    assert _run(["friction", "-v"]) == 0
    text = capsys.readouterr().out
    assert friction_id("harness", SHARED["summary"]) in text
    assert "Shared across agents" in text and "needs from_line" in text


def test_offline_is_an_error_not_an_empty_list(monkeypatch, capsys):
    def boom(**kw):
        raise deckhub.NoHub(["http://127.0.0.1:7870"], [], None, None)

    monkeypatch.setattr(deckhub, "connect", boom)
    assert _run(["friction"]) == 1
    assert "needs a running hub" in capsys.readouterr().err


# ── resolve ──────────────────────────────────────────────────────────────────


def test_resolve_has_no_default_scope(monkeypatch, capsys):
    c = FakeClient()
    _live(monkeypatch, c)
    assert _run(["friction", "resolve", friction_id("harness", SHARED["summary"])]) == 1
    assert "no default scope" in capsys.readouterr().err
    assert not [call for call in c.calls if call[0] == "post"]


def test_resolve_named_agent_stamps_exactly_that_row(monkeypatch, capsys):
    c = FakeClient()
    _live(monkeypatch, c)
    sid = friction_id("harness", SHARED["summary"])
    assert _run(["friction", "resolve", sid, "--agent", "alpha", "--reason", "fixed in #1", "--json"]) == 0
    posts = [call for call in c.calls if call[0] == "post"]
    assert [p[1] for p in posts] == ["alpha-1"]  # beta has the same signal and is untouched
    assert posts[0][3] == {"summary": SHARED["summary"], "kind": "harness", "reason": "fixed in #1", "resolved": True}


def test_resolve_all_agents_hits_every_member_that_has_it(monkeypatch, capsys):
    c = FakeClient()
    _live(monkeypatch, c)
    assert _run(["friction", "resolve", friction_id("harness", SHARED["summary"]), "--all-agents", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(r["name"] for r in out["results"]) == ["alpha", "beta"]  # hub/idle don't have it → skipped


def test_resolve_reopen_clears_the_stamp(monkeypatch, capsys):
    c = FakeClient()
    _live(monkeypatch, c)
    assert _run(["friction", "resolve", friction_id("harness", SHARED["summary"]), "--agent", "alpha", "--reopen", "--json"]) == 0
    assert [call for call in c.calls if call[0] == "post"][0][3]["resolved"] is False
    assert any(call[0] == "get" and call[3].get("resolved") == "true" for call in c.calls)


def test_resolve_unknown_id_on_a_named_agent_fails_loudly(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    assert _run(["friction", "resolve", "deadbeef", "--agent", "alpha", "--json"]) == 1
    assert "no open signal deadbeef" in json.loads(capsys.readouterr().out)["results"][0]["error"]


def test_a_mistyped_agent_is_an_error_not_an_empty_result(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    assert _run(["friction", "--json", "--agent", "alpa"]) == 1
    row = json.loads(capsys.readouterr().out)["results"][0]
    assert row["name"] == "alpa" and row["ok"] is False and "no such member" in row["error"]


def test_resolve_with_nothing_to_resolve_still_emits_json(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    assert _run(["friction", "resolve", "deadbeef", "--all-agents", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["results"] == []
