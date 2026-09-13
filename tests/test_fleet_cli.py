"""``protoagent fleet`` — live vs offline mode, ``--json``, and routing (#3467, epic #3466).

The hub client is stubbed at ``deck.hub.connect``; the supervisor at ``graph.fleet.supervisor``.
What is under test is the CLI's choice of path (hub REST vs disk), what it prints, and that
``--json`` carries the same dicts the routes return plus ``mode``/``hub``.
"""

from __future__ import annotations

import json

import pytest

from deck import hub as deckhub
from graph.fleet import cli

ROSTER = [
    {"name": "protoagent", "label": "protoagent", "id": "protoagent", "port": 7870, "pid": 100, "running": True, "host": True, "version": "0.165.0", "bundle": ""},
    {"name": "protoEngineer", "label": "protoEngineer", "id": "protoEngineer-ba4c", "port": 7875, "pid": 15285, "running": True, "version": "0.165.0", "bundle": ""},
    {"name": "Cindi", "label": "Cindi", "id": "Cindi-9f49", "port": 7880, "pid": None, "running": False, "version": "", "bundle": "cowork-stack"},
    {"name": "old", "label": "old", "id": "old-1", "port": 7890, "pid": 77, "running": True, "version": "0.164.0", "bundle": ""},
    {"name": "ava", "label": "ava", "id": "r-ava", "port": None, "pid": None, "running": False, "remote": True, "url": "https://ava.tail:7870", "version": ""},
]


class FakeClient:
    def __init__(self, roster=None, *, start=None, stop=None, down=None):
        self.url = "http://127.0.0.1:7870"
        self.roster = list(ROSTER if roster is None else roster)
        self.calls: list[tuple] = []
        self._start = start or (lambda n: {"ok": True, "agent": {"name": n, "port": 7999, "pid": 4242}})
        self._stop = stop or (lambda n: {"ok": True, "stopped": True, "name": n})
        self._down = down or (lambda: {"ok": True, "stopped": ["protoEngineer", "old"]})
        self.closed = False

    def fleet(self):
        self.calls.append(("fleet",))
        return list(self.roster)

    def start(self, name):
        self.calls.append(("start", name))
        return self._start(name)

    def stop(self, name):
        self.calls.append(("stop", name))
        return self._stop(name)

    def down(self, running=0):
        self.calls.append(("down", running))
        return self._down()

    def close(self):
        self.closed = True


def _live(monkeypatch, client: FakeClient, source="heartbeat"):
    cand = deckhub.HubCandidate(client.url, source)
    conn = deckhub.Connection(client=client, candidate=cand, card={"name": "protoagent"}, roster=list(client.roster))
    seen: dict = {}

    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        seen["url"], seen["token"] = url, token
        return conn

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    return seen


def _offline(monkeypatch, *, unauthorized=None, failed=None, members=None):
    """Nothing answered (the default) → the CLI may fall back to disk. Pass unauthorized /
    failed / members to simulate a hub that ANSWERED but could not be opened."""

    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        raise deckhub.NoHub(["http://127.0.0.1:7870"], unauthorized or [], members, failed)

    monkeypatch.setattr(deckhub, "connect", fake_connect)


@pytest.fixture
def sup(monkeypatch):
    """A supervisor whose disk view carries the synthesized host row (as the real one does)."""
    calls: list[tuple] = []
    disk = [
        {"name": "main", "label": "main", "id": "main", "port": 7870, "pid": 999, "running": True, "host": True, "version": "0.165.0", "bundle": ""},
        {"name": "alpha", "label": "alpha", "id": "alpha-1", "port": 7901, "pid": None, "running": False, "version": "", "bundle": ""},
    ]
    monkeypatch.setattr(cli.supervisor, "status", lambda: list(disk))
    monkeypatch.setattr(cli.supervisor, "up", lambda names=None: calls.append(("up", names)) or [{"name": "alpha", "port": 7901, "pid": 5}])
    monkeypatch.setattr(cli.supervisor, "down", lambda names=None: calls.append(("down", names)) or [{"name": "alpha", "stopped": True}])
    return calls


# ── the deck entry (bare `fleet`, `top`) ──────────────────────────────────────


def test_bare_fleet_needs_a_terminal_on_both_ends(monkeypatch, capsys):
    import importlib

    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **kw: (_ for _ in ()).throw(AssertionError("must not import the deck")) if name.startswith("deck.") else real(name, *a, **kw))
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.run_fleet_cli([]) == 2
    assert "needs a terminal" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.run_fleet_cli([]) == 2


def test_bare_fleet_json_is_the_roster_not_a_tui(monkeypatch, capsys):
    """Review MEDIUM-3: `fleet --json` on a pipe used to start Textual against the pipe and hang."""
    _live(monkeypatch, FakeClient())
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.run_fleet_cli(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "live"
    assert cli.run_deck_cli(["ls", "--json"]) == 0  # `top ls --json` → the roster too


def test_bare_fleet_without_the_deck_module_prints_a_hint(monkeypatch, capsys):
    """A frozen build that does not bundle Textual (S6's call) must say so, not traceback."""
    import importlib

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    real = importlib.import_module

    def fake(name, *a, **kw):
        if name.startswith("deck.app"):
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake)
    assert cli.run_fleet_cli([]) == 2
    assert "not available in this build" in capsys.readouterr().err


def test_bare_fleet_opens_the_deck_on_the_live_backend(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    seen: dict = {}
    _live(monkeypatch, FakeClient())

    class FakeApp:
        @staticmethod
        def run(backend):
            seen["mode"] = backend.mode
            return 0

    import importlib

    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **kw: FakeApp if name == "deck.app" else real(name, *a, **kw))
    assert cli.run_fleet_cli([]) == 0
    assert seen == {"mode": "live"}
    assert cli.run_deck_cli(["ls"]) == 0  # `top` strips a LEADING verb and opens the deck
    assert seen == {"mode": "live"}
    # ...but never an option VALUE that happens to spell a verb (CodeRabbit)
    seen.clear()
    tokens: dict = {}

    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        tokens["token"] = token
        client = FakeClient()
        return deckhub.Connection(client=client, candidate=deckhub.HubCandidate(client.url, "flag"), card={}, roster=list(client.roster))

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    assert cli.run_deck_cli(["--token", "status"]) == 0
    assert tokens == {"token": "status"}


def test_flags_work_before_and_after_the_verb(monkeypatch, capsys):
    _live(monkeypatch, FakeClient())
    assert cli.run_fleet_cli(["--json", "ls"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "live"
    assert cli.run_fleet_cli(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "live"
    seen = _live(monkeypatch, FakeClient())
    cli.run_fleet_cli(["--hub", "x:1", "ls", "--token", "t"])
    assert seen == {"url": "x:1", "token": "t"}


# ── presence vocabulary ───────────────────────────────────────────────────────


def test_presence_words_match_the_console():
    assert [cli.presence_of(a) for a in ROSTER] == ["host", "online", "stopped", "online", "unreachable"]
    assert cli.presence_of({"running": True, "remote": True}) == "remote"


# ── ls ───────────────────────────────────────────────────────────────────────


def test_ls_live_renders_hub_header_rows_and_skew(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["ls"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "protoagent fleet · live · http://127.0.0.1:7870 · protoagent v0.165.0 · via heartbeat"
    assert "● protoagent" in out and "host" in out
    assert "● protoEngineer" in out and ":7875" in out and "pid 15285" in out
    assert "○ Cindi" in out and "stopped" in out and "[cowork-stack]" in out
    assert "v0.164.0 !skew" in out  # a running member on another version is flagged
    assert "◌ ava" in out and "unreachable" in out and "https://ava.tail:7870" in out
    assert "!skew" not in out.split("protoEngineer")[1].split("\n")[0]
    assert client.closed


def test_ls_live_json_carries_route_dicts_plus_mode(monkeypatch, capsys):
    _live(monkeypatch, FakeClient(), source="pidfile")
    assert cli.run_fleet_cli(["ls", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "live" and data["hub"] == "http://127.0.0.1:7870" and data["via"] == "pidfile"
    assert data["agents"] == ROSTER


def test_ls_offline_drops_the_fabricated_host_row_and_badges_disk(monkeypatch, capsys, sup, tmp_path):
    _offline(monkeypatch)
    monkeypatch.setattr("graph.workspaces.manager.workspaces_root", lambda: tmp_path / "ws")
    assert cli.run_fleet_cli(["ls"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == f"protoagent fleet · offline · reading {tmp_path / 'ws' / 'fleet.json'}"
    assert "main" not in captured.out  # the CLI's own pid is not a server
    assert "○ alpha" in captured.out
    assert "no hub answered" in captured.err


def test_ls_offline_json(monkeypatch, capsys, sup, tmp_path):
    _offline(monkeypatch)
    monkeypatch.setattr("graph.workspaces.manager.workspaces_root", lambda: tmp_path / "ws")
    assert cli.run_fleet_cli(["ls", "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["mode"] == "offline"
    assert data["fleet_json"].endswith("fleet.json")
    assert [a["name"] for a in data["agents"]] == ["alpha"]
    assert captured.err == ""  # --json keeps stderr quiet too


def test_ls_offline_flag_never_probes(monkeypatch, capsys, sup, tmp_path):
    def boom(**kw):
        raise AssertionError("--offline must not call connect")

    monkeypatch.setattr(deckhub, "connect", boom)
    monkeypatch.setattr("graph.workspaces.manager.workspaces_root", lambda: tmp_path / "ws")
    assert cli.run_fleet_cli(["ls", "--offline"]) == 0
    assert "offline" in capsys.readouterr().out


def test_ls_explicit_hub_that_fails_is_an_error_not_a_fallback(monkeypatch, capsys, sup):
    _offline(monkeypatch, unauthorized=["http://ava.tail:7870"])
    assert cli.run_fleet_cli(["ls", "--hub", "ava.tail:7870"]) == 1
    captured = capsys.readouterr()
    assert "rejected every credential" in captured.err
    assert "alpha" not in captured.out


@pytest.mark.parametrize(
    "kw",
    [
        {"failed": {"http://127.0.0.1:7870": "http://127.0.0.1:7870 did not answer (ReadTimeout)"}},
        {"unauthorized": ["http://127.0.0.1:7870"]},
        {"members": ["http://127.0.0.1:7871"]},
    ],
)
def test_a_hub_that_answered_but_could_not_be_opened_never_falls_back_to_disk(monkeypatch, capsys, sup, kw):
    """The two-hubs rule, on the FAILING path (review HIGH-1): a slow/500/403 hub used to
    read as "no hub", and `fleet up` then drove the supervisor beside the running hub."""
    _offline(monkeypatch, **kw)
    assert cli.run_fleet_cli(["up", "alpha"]) == 1
    assert sup == []  # the supervisor was never touched
    assert cli.run_fleet_cli(["down"]) == 1
    assert sup == []
    assert cli.run_fleet_cli(["ls"]) == 1
    captured = capsys.readouterr()
    assert "alpha" not in captured.out
    assert "✗" in captured.err


def test_offline_and_hub_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.run_fleet_cli(["ls", "--offline", "--hub", "x:1"])
    assert ei.value.code == 2


def test_malformed_hub_is_a_clean_exit(monkeypatch, capsys):
    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        raise ValueError("invalid hub url: 'http://[::1'")

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    assert cli.run_fleet_cli(["ls", "--hub", "http://[::1"]) == 1
    assert "invalid hub url" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_malformed_hub_with_userinfo_never_echoes_the_secret(capsys, flags):
    """Round-2 blocker, through the real entrypoint: the raw --hub reached the error
    message AND the JSON `hub` field."""
    assert cli.run_fleet_cli(["ls", "--hub", "ftp://user:s3cret@host", *flags]) == 1
    captured = capsys.readouterr()
    assert "s3cret" not in captured.out and "s3cret" not in captured.err
    if flags:
        data = json.loads(captured.out)
        assert data["mode"] == "error" and "***@" in data["hub"] and "s3cret" not in json.dumps(data)


def test_unreachable_hub_with_userinfo_never_echoes_the_secret(monkeypatch, capsys):
    """The NoHub path carries the normalized url (userinfo already stripped); the CLI must
    not reintroduce the raw --hub anywhere."""

    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        raise deckhub.NoHub([deckhub.normalize_url(url)], [])

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    assert cli.run_fleet_cli(["ls", "--hub", "http://user:s3cret@127.0.0.1:7999", "--json"]) == 1
    captured = capsys.readouterr()
    assert "s3cret" not in captured.out and "s3cret" not in captured.err
    assert json.loads(captured.out)["hub"] == "http://127.0.0.1:7999"


def test_ls_passes_hub_token_and_insecure_flag_through(monkeypatch, capsys):
    seen: dict = {}

    def fake_connect(*, url=None, token=None, candidates=None, transport=None, insecure_http=False):
        seen.update(url=url, token=token, insecure_http=insecure_http)
        client = FakeClient()
        return deckhub.Connection(client=client, candidate=deckhub.HubCandidate(client.url, "flag"), card={}, roster=list(client.roster))

    monkeypatch.setattr(deckhub, "connect", fake_connect)
    cli.run_fleet_cli(["status", "--hub", "ava.tail:7870", "--token", "abc"])
    assert seen == {"url": "ava.tail:7870", "token": "abc", "insecure_http": False}
    assert "abc" not in capsys.readouterr().out
    cli.run_fleet_cli(["status", "--hub", "ava.tail:7870", "--token", "abc", "--insecure-http"])
    assert seen["insecure_http"] is True


def test_offline_supervisor_error_is_json_when_asked(monkeypatch, capsys, sup):
    _offline(monkeypatch)
    monkeypatch.setattr(cli.supervisor, "status", lambda: (_ for _ in ()).throw(cli.supervisor.FleetError("fleet.json is corrupt")))
    assert cli.run_fleet_cli(["ls", "--offline", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "error" and "corrupt" in data["error"]


# ── up ───────────────────────────────────────────────────────────────────────


def test_up_live_starts_named_through_the_hub(monkeypatch, capsys, sup):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["up", "Cindi"]) == 0
    assert client.calls == [("start", "Cindi")]
    assert sup == []  # never the supervisor beside a running hub
    out = capsys.readouterr().out
    assert "✓ Cindi" in out and "via hub http://127.0.0.1:7870" in out


def test_up_live_all_means_every_local_stopped_member(monkeypatch, capsys, sup):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["up"]) == 0
    assert client.calls == [("start", "Cindi")]  # not host, not remote, not running — from the connect roster


def test_up_live_failure_is_reported_per_member_with_exit_1(monkeypatch, capsys, sup):
    def start(name):
        raise deckhub.HubRequestError("http://127.0.0.1:7870", 400, f"unknown agent {name!r}")

    client = FakeClient(start=start)
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["up", "ghost"]) == 1
    assert "✗ ghost" in capsys.readouterr().err


def test_up_live_keeps_partial_results_when_one_start_times_out(monkeypatch, capsys, sup):
    """Review HIGH-2: a boot-watch that outlasts the read budget must not discard the
    results already collected for the other members."""

    def start(name):
        if name == "slow":
            raise deckhub.HubUnreachable("http://127.0.0.1:7870", "http://127.0.0.1:7870 did not answer (ReadTimeout)")
        return {"ok": True, "agent": {"name": name, "port": 7999, "pid": 1}}

    client = FakeClient(start=start)
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["up", "Cindi", "slow", "Claudia", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert [(r["name"], r["ok"]) for r in data["results"]] == [("Cindi", True), ("slow", False), ("Claudia", True)]
    assert "ReadTimeout" in data["results"][1]["error"]


def test_up_live_json(monkeypatch, capsys, sup):
    _live(monkeypatch, FakeClient())
    assert cli.run_fleet_cli(["up", "Cindi", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "live" and data["hub"] == "http://127.0.0.1:7870"
    assert data["results"] == [{"name": "Cindi", "ok": True, "agent": {"name": "Cindi", "port": 7999, "pid": 4242}}]


def test_up_offline_uses_the_supervisor(monkeypatch, capsys, sup):
    _offline(monkeypatch)
    assert cli.run_fleet_cli(["up", "alpha"]) == 0
    assert sup == [("up", ["alpha"])]
    assert "via disk (offline)" in capsys.readouterr().out


# ── down ─────────────────────────────────────────────────────────────────────


def test_down_live_all_uses_the_fleet_down_route_with_a_budget_per_running_member(monkeypatch, capsys, sup):
    client = FakeClient(down=lambda: {"ok": False, "stopped": ["protoEngineer"], "failed": [{"name": "old", "reason": "survived SIGKILL"}]})
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["down", "--json"]) == 1
    assert client.calls == [("down", 2)]  # protoEngineer + old are the running LOCAL members
    data = json.loads(capsys.readouterr().out)
    # one row shape for every mode: {name, ok, ...}
    assert data["results"] == [
        {"name": "protoEngineer", "ok": True, "stopped": True},
        {"name": "old", "ok": False, "error": "survived SIGKILL", "stopped": False},
    ]


def test_down_live_named_reports_a_survivor_as_failure(monkeypatch, capsys, sup):
    client = FakeClient(stop=lambda n: {"ok": False, "stopped": False, "reason": "still alive after SIGKILL"})
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["down", "old"]) == 1
    captured = capsys.readouterr()
    assert "✗ old" in captured.err and "not stopped — still alive after SIGKILL" in captured.err
    assert captured.out == ""  # failures go to stderr; stdout stays clean for pipelines


def test_down_offline_uses_the_supervisor_and_json(monkeypatch, capsys, sup):
    _offline(monkeypatch)
    assert cli.run_fleet_cli(["down", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "offline" and data["results"] == [{"name": "alpha", "ok": True, "stopped": True}]
    assert sup == [("down", None)]


def test_down_offline_honours_a_survivor_and_an_unknown_name(monkeypatch, capsys, sup):
    """Review MEDIUM-5: offline `down` printed ✓ and exited 0 for a member that survived
    SIGKILL, and swallowed a name the supervisor did not know."""
    _offline(monkeypatch)
    monkeypatch.setattr(cli.supervisor, "down", lambda names=None: [{"name": "alpha", "stopped": False, "reason": "still alive after SIGKILL"}])
    assert cli.run_fleet_cli(["down", "alpha", "ghost"]) == 1
    captured = capsys.readouterr()
    assert "✗ alpha" in captured.err and "still alive after SIGKILL" in captured.err
    assert "✗ ghost" in captured.err
    assert "✓" not in captured.out
