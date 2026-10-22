"""``protoagent usage`` — subscription usage over a fake server/hub, no network (#4433).

The live server/hub is stubbed at ``deck.hub.connect`` (the same seam as test_fleet_cli /
test_fleet_friction_cli), and ``usage_cli._now`` is pinned so reset countdowns are
deterministic. Under test: the per-account/per-window table (scoped windows labelled by
model, ``unknown`` accounts), the stable ``--json`` schema, ``--fleet`` dedupe by
account_key with the members listed, an unreachable member shown beside a reachable one,
the exit codes 0/2/1, ``--account`` filtering, and a stale account whose last-known windows
are marked ``stale`` and never trip ``--warn-at``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from deck import hub as deckhub
from graph import usage_cli as cli

NOW = "2026-10-22T10:00:00+00:00"
# session resets +2h13m, weekly resets +3d from NOW.
SESSION_RESET = "2026-10-22T12:13:00+00:00"
WEEKLY_RESET = "2026-10-25T10:00:00+00:00"


def win(wid, kind, used, resets_at, status="ok", scope=None, window_seconds=18000, binding=False):
    return {
        "id": wid, "kind": kind, "window_seconds": window_seconds, "used_pct": used,
        "resets_at": resets_at, "status": status, "scope": scope, "binding": binding,
    }


def acct(provider_id, *, account_key="acct-1", plan="pro", status="ok", windows=(), stale=False, error=None):
    return {
        "provider_id": provider_id, "provider_type": provider_id, "account_key": account_key,
        "plan": plan, "source": "endpoint", "fetched_at": NOW, "stale_after": NOW,
        "windows": list(windows), "credits": None, "status": status, "error": error,
        "retry_after": None, "stale": stale,
    }


# A rich anthropic account (a session window + a model-scoped weekly window) and a
# disconnected codex account that reads as unknown.
ANTHROPIC = acct(
    "anthropic-oauth", account_key="acct-1", plan="pro",
    windows=[
        win("session", "session", 42.0, SESSION_RESET),
        win("weekly", "weekly", 88.0, WEEKLY_RESET, status="warning", scope={"model": "opus"}),
    ],
)
CODEX_UNKNOWN = acct("openai-codex", account_key=None, plan=None, status="unknown", windows=[], error="not connected")

ROSTER = [
    {"name": "hub", "label": "hub", "id": "hub", "running": True, "host": True},
    {"name": "alpha", "label": "alpha", "id": "alpha-1", "running": True},
    {"name": "beta", "label": "beta", "id": "beta-2", "running": True},
    {"name": "idle", "label": "idle", "id": "idle-3", "running": False},
]


class FakeClient:
    url = "http://127.0.0.1:7870"

    def __init__(self, usage, roster=None, fail=None):
        self.usage = {k: list(v) for k, v in usage.items()}
        self.roster = list(ROSTER if roster is None else roster)
        self.fail = fail or {}
        self.calls = []

    def member_get(self, slug, rel, **params):
        self.calls.append((slug, rel, params))
        if slug in self.fail:
            raise self.fail[slug]
        return {"accounts": list(self.usage.get(slug, [])), "fetched_at": NOW}

    def close(self):
        pass


def _live(monkeypatch, client):
    conn = deckhub.Connection(
        client=client,
        candidate=deckhub.HubCandidate(client.url, "heartbeat"),
        card={"name": "hub"},
        roster=list(client.roster),
    )
    monkeypatch.setattr(deckhub, "connect", lambda **kw: conn)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC))


def _run(argv):
    return cli.run_usage_cli(argv)


# ── r1: local table ──────────────────────────────────────────────────────────


def test_local_table_has_a_row_per_window_with_scoped_label_and_unknown(monkeypatch, capsys):
    _live(monkeypatch, FakeClient({"host": [ANTHROPIC, CODEX_UNKNOWN]}))
    assert _run(["--warn-at", "100"]) == 0
    out = capsys.readouterr().out
    assert "anthropic-oauth" in out and "openai-codex" in out
    assert "session" in out and "42%" in out
    assert "weekly·opus" in out  # a scoped window labelled by model (ADR 0122)
    assert "88%" in out
    assert "2h13m" in out and "3d" in out  # reset countdowns, from the pinned clock
    assert "unknown" in out  # the disconnected codex account
    assert out.count("anthropic-oauth") == 2  # one row per window


def test_local_reads_only_the_host_slug(monkeypatch):
    c = FakeClient({"host": [ANTHROPIC]})
    _live(monkeypatch, c)
    _run(["--json"])
    assert {slug for slug, _rel, _p in c.calls} == {"host"}


# ── r4: stable --json schema ───────────────────────────────────────────────────


TOP_KEYS = {"mode", "ok", "hub", "warn_at", "warn_hit", "error", "accounts", "unreachable"}
ACCOUNT_KEYS = {"provider_id", "provider_type", "account_key", "plan", "status", "stale", "error", "members", "windows"}
WINDOW_KEYS = {
    "id", "kind", "label", "scope", "used_pct", "status", "resets_at",
    "resets_in_seconds", "resets_in", "window_seconds", "binding", "over_warn",
}


def test_json_schema_is_stable(monkeypatch, capsys):
    _live(monkeypatch, FakeClient({"host": [ANTHROPIC, CODEX_UNKNOWN]}))
    _run(["--json"])
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == TOP_KEYS
    assert doc["mode"] == "local" and doc["ok"] is True
    assert doc["hub"] == "http://127.0.0.1:7870"
    assert doc["warn_at"] == 80.0
    for a in doc["accounts"]:
        assert set(a) == ACCOUNT_KEYS
        for w in a["windows"]:
            assert set(w) == WINDOW_KEYS
    anthropic = next(a for a in doc["accounts"] if a["provider_id"] == "anthropic-oauth")
    session = next(w for w in anthropic["windows"] if w["id"] == "session")
    assert session["label"] == "session" and session["resets_in"] == "2h13m" and session["resets_in_seconds"] == 7980
    weekly = next(w for w in anthropic["windows"] if w["id"] == "weekly")
    assert weekly["label"] == "weekly·opus" and weekly["scope"] == {"model": "opus"}
    codex = next(a for a in doc["accounts"] if a["provider_id"] == "openai-codex")
    assert codex["status"] == "unknown" and codex["windows"] == [] and codex["members"] == []


# ── r2: --fleet dedupe + unreachable ────────────────────────────────────────────


def test_fleet_dedupes_two_members_on_one_account_key(monkeypatch, capsys):
    # hub and alpha share acct-1; beta is a different account (acct-2).
    beta_acct = acct("anthropic-oauth", account_key="acct-2", windows=[win("session", "session", 10.0, SESSION_RESET)])
    c = FakeClient({"host": [ANTHROPIC], "alpha-1": [ANTHROPIC], "beta-2": [beta_acct]})
    _live(monkeypatch, c)
    assert _run(["--fleet", "--warn-at", "100", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "fleet"
    by_key = {a["account_key"]: a for a in doc["accounts"]}
    assert set(by_key) == {"acct-1", "acct-2"}
    assert sorted(by_key["acct-1"]["members"]) == ["alpha", "hub"]  # the two that share it
    assert by_key["acct-2"]["members"] == ["beta"]
    # the stopped member is read from neither, but is named as unreachable
    assert {u["member"] for u in doc["unreachable"]} == {"idle"}


def test_fleet_unreachable_member_shown_beside_a_reachable_one(monkeypatch, capsys):
    fail = {"beta-2": deckhub.HubRequestError("http://127.0.0.1:7870", 500, "boom")}
    c = FakeClient({"host": [ANTHROPIC], "alpha-1": [ANTHROPIC]}, fail=fail)
    _live(monkeypatch, c)
    assert _run(["--fleet", "--warn-at", "100", "--json"]) == 0  # one member failing is not a global error
    doc = json.loads(capsys.readouterr().out)
    assert [a["account_key"] for a in doc["accounts"]] == ["acct-1"]
    assert sorted(doc["accounts"][0]["members"]) == ["alpha", "hub"]
    reasons = {u["member"]: u["error"] for u in doc["unreachable"]}
    assert "beta" in reasons and "boom" in reasons["beta"]
    assert reasons["idle"] == "stopped"


def test_fleet_keyless_accounts_stay_distinct_and_a_hot_one_is_not_hidden(monkeypatch, capsys):
    # Claude accounts authed from an env-var token (or borrowed Claude Code credentials) have
    # NO account_key. Two different members' keyless accounts must NOT collapse into one
    # provider-id row: that would let _best_account keep one member's windows and drop the
    # rest — hiding a member at/over --warn-at (wrong exit 0) and falsely showing them as
    # sharing one account.
    cool = acct("anthropic-oauth", account_key=None, windows=[win("session", "session", 30.0, SESSION_RESET)])
    hot = acct("anthropic-oauth", account_key=None, windows=[win("session", "session", 90.0, SESSION_RESET)])
    roster = [
        {"name": "hub", "label": "hub", "id": "hub", "running": True, "host": True},
        {"name": "alpha", "label": "alpha", "id": "alpha-1", "running": True},
    ]
    c = FakeClient({"host": [cool], "alpha-1": [hot]}, roster=roster)
    _live(monkeypatch, c)
    assert _run(["--fleet", "--json"]) == 2  # the 90% window trips the default --warn-at 80
    doc = json.loads(capsys.readouterr().out)
    assert doc["warn_hit"] is True
    assert len(doc["accounts"]) == 2  # two distinct rows, not one merged row
    assert sorted(a["members"] for a in doc["accounts"]) == [["alpha"], ["hub"]]  # each names only its own member
    assert sorted(w["used_pct"] for a in doc["accounts"] for w in a["windows"]) == [30.0, 90.0]  # no window dropped


def test_fleet_keyless_accounts_on_one_member_stay_distinct(monkeypatch, capsys):
    # Even on a single member, two keyless accounts of one provider are different accounts and
    # must not merge into each other.
    a1 = acct("anthropic-oauth", account_key=None, windows=[win("session", "session", 12.0, SESSION_RESET)])
    a2 = acct("anthropic-oauth", account_key=None, windows=[win("session", "session", 34.0, SESSION_RESET)])
    roster = [{"name": "hub", "label": "hub", "id": "hub", "running": True, "host": True}]
    _live(monkeypatch, FakeClient({"host": [a1, a2]}, roster=roster))
    assert _run(["--fleet", "--warn-at", "100", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["accounts"]) == 2
    assert sorted(w["used_pct"] for a in doc["accounts"] for w in a["windows"]) == [12.0, 34.0]


# ── r3: exit codes ──────────────────────────────────────────────────────────────


def test_exit_2_when_a_window_is_at_or_above_warn_at(monkeypatch, capsys):
    hot = acct("anthropic-oauth", windows=[win("session", "session", 85.0, SESSION_RESET)])
    _live(monkeypatch, FakeClient({"host": [hot]}))
    assert _run([]) == 2
    assert "85%!" in capsys.readouterr().out  # the over-warn marker


def test_exit_0_when_warn_at_raised_above_usage(monkeypatch, capsys):
    hot = acct("anthropic-oauth", windows=[win("session", "session", 85.0, SESSION_RESET)])
    _live(monkeypatch, FakeClient({"host": [hot]}))
    assert _run(["--warn-at", "90", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["warn_hit"] is False
    assert doc["accounts"][0]["windows"][0]["over_warn"] is False


def test_stale_account_is_marked_and_never_trips_warn(monkeypatch, capsys):
    # usage_store._present keeps the last-known windows on a failed poll but forces the
    # account status to "unknown" and stale=True. Those numbers are not live: a 95% window
    # must NOT trip the default --warn-at 80, and the row must be marked "stale", not shown
    # with its old live-looking per-window status.
    stale = acct(
        "anthropic-oauth", status="unknown", stale=True,
        windows=[win("session", "session", 95.0, SESSION_RESET, status="ok")],
    )
    _live(monkeypatch, FakeClient({"host": [stale]}))
    assert _run([]) == 0  # default warn-at 80, but stale data never trips it
    out = capsys.readouterr().out
    assert "95%" in out  # the last-known number is still shown…
    assert "stale" in out  # …but the row's status is marked stale, not its old live value
    assert "95%!" not in out  # and it carries no over-warn marker


def test_json_stale_account_suppresses_over_warn(monkeypatch, capsys):
    stale = acct(
        "anthropic-oauth", status="unknown", stale=True,
        windows=[win("weekly", "weekly", 95.0, WEEKLY_RESET, status="warning")],
    )
    _live(monkeypatch, FakeClient({"host": [stale]}))
    assert _run(["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["warn_hit"] is False
    a = doc["accounts"][0]
    assert a["stale"] is True and a["status"] == "unknown"
    assert a["windows"][0]["over_warn"] is False


def test_exit_1_when_no_server_answers(monkeypatch, capsys):
    monkeypatch.setattr(deckhub, "connect", lambda **kw: (_ for _ in ()).throw(deckhub.NoHub(["127.0.0.1:7870"], [])))
    assert _run([]) == 1
    assert "needs a running server" in capsys.readouterr().err


def test_exit_1_when_no_hub_answers_for_fleet_json(monkeypatch, capsys):
    monkeypatch.setattr(deckhub, "connect", lambda **kw: (_ for _ in ()).throw(deckhub.NoHub(["127.0.0.1:7870"], [])))
    assert _run(["--fleet", "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["mode"] == "fleet" and doc["error"]


# ── --account filtering ─────────────────────────────────────────────────────────


def test_account_filter_by_provider_id(monkeypatch, capsys):
    _live(monkeypatch, FakeClient({"host": [ANTHROPIC, CODEX_UNKNOWN]}))
    _run(["--account", "openai-codex", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert [a["provider_id"] for a in doc["accounts"]] == ["openai-codex"]


def test_account_filter_by_account_key(monkeypatch, capsys):
    _live(monkeypatch, FakeClient({"host": [ANTHROPIC, CODEX_UNKNOWN]}))
    _run(["--account", "acct-1", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert [a["account_key"] for a in doc["accounts"]] == ["acct-1"]


# ── r1 (continued): listed in `protoagent --help` ───────────────────────────────


def test_usage_is_registered_and_listed_in_help(capsys):
    from server import cli as servercli

    assert servercli._FORWARD["usage"] == ("graph.usage_cli", "run_usage_cli")
    assert servercli.main(["--help"]) == 0
    assert "Subscription usage" in capsys.readouterr().out
