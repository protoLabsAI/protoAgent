"""``protoagent fleet`` — live vs offline mode, ``--json``, and routing (#3467, epic #3466).

The hub client is stubbed at ``deck.hub.connect``; the supervisor at ``graph.fleet.supervisor``.
What is under test is the CLI's choice of path (hub REST vs disk), what it prints, and that
``--json`` carries the same dicts the routes return plus ``mode``/``hub``.
"""

from __future__ import annotations

import json
from pathlib import Path

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

    # ── manage (#3471) ──
    remove_status: int | None = None  # a HubRequestError status to raise on remove

    def archetypes(self):
        self.calls.append(("archetypes",))
        return [{"id": "basic", "label": "Basic", "bundle": None, "soul": ""}, {"id": "pm", "label": "PM", "bundle": "https://github.com/x/pm-archetype", "soul": "You are a PM.", "requires_tools": ["github.write"]}]

    def create(self, body):
        self.calls.append(("create", dict(body)))
        return {"ok": True, "agent": {"name": body["name"], "id": f"{body['name']}-1", "port": body.get("port") or 7999, "pid": 4242 if body.get("start", True) else None, "running": body.get("start", True)}, "installed": ["pm"] if body.get("bundle") else [], "warnings": ["credential store stayed local"] if body.get("bundle") else []}

    def rename(self, ident, name):
        self.calls.append(("rename", ident, name))
        return {"ok": True, "id": "alpha-1", "name": name}

    def remove(self, ident, *, purge=False):
        self.calls.append(("remove", ident, purge))
        if self.remove_status:
            raise deckhub.HubRequestError(self.url, self.remove_status, "workspace busy" if self.remove_status == 409 else "no such member")
        return {"ok": True, "name": ident, "removed": ["workspace"] if purge else []}

    def remote_add(self, name, url, token=""):
        self.calls.append(("remote_add", name, url, token))
        return {"ok": True, "agent": {"id": f"r-{name}", "name": name, "url": url, "remote": True}, "reachable": False, "version": ""}

    def remote_update(self, ident, **fields):
        self.calls.append(("remote_update", ident, fields))
        return {"ok": True, "agent": {"id": ident, "name": "ava", "url": fields.get("url", "https://ava.tail:7870"), "remote": True}, "reachable": True, "version": "0.165.0"}

    def remote_remove(self, ident):
        self.calls.append(("remote_remove", ident))
        return {"ok": True, "id": ident, "name": "ava", "removed": ["remote"]}

    def set_order(self, ids):
        self.calls.append(("set_order", list(ids)))
        return {"ok": True, "order": list(ids)}

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
    seen_kw: dict = {}
    _live(monkeypatch, FakeClient())

    class FakeApp:
        @staticmethod
        def run(backend, **kw):
            seen_kw.update(kw)
            seen["mode"] = backend.mode
            return 0

    import importlib

    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **kw: FakeApp if name == "deck.app" else real(name, *a, **kw))
    assert cli.run_fleet_cli([]) == 0
    assert seen_kw["start_on_hubs"] is False and callable(seen_kw["launcher"]) and callable(seen_kw["peers"]) and seen_kw["token"] is None
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


# ── manage verbs (#3471): live through the hub, offline through the ops, --json on each ──


def test_new_live_resolves_an_archetype_on_the_hub_and_posts_the_consoles_body(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["new", "scout", "--archetype", "pm", "--no-start", "--port", "7911", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    create = next(c for c in client.calls if c[0] == "create")[1]
    assert create == {"name": "scout", "start": False, "inherit_config": True, "port": 7911, "bundle": "https://github.com/x/pm-archetype", "soul": "You are a PM.", "requires_tools": ["github.write"]}
    assert body["mode"] == "live" and body["results"][0]["ok"] and body["results"][0]["installed"] == ["pm"] and body["results"][0]["agent"]["running"] is False
    # an unknown archetype is refused before anything is created
    client.calls.clear()
    assert cli.run_fleet_cli(["new", "scout", "--archetype", "nope"]) == 1
    assert not any(c[0] == "create" for c in client.calls) and "no archetype 'nope'" in capsys.readouterr().err
    # a blank member, no inheritance
    assert cli.run_fleet_cli(["new", "blank", "--no-inherit"]) == 0
    assert next(c for c in client.calls if c[0] == "create")[1] == {"name": "blank", "start": True, "inherit_config": False}
    assert "started (:7999, pid 4242) via hub" in capsys.readouterr().out


def test_new_offline_runs_the_op_and_refuses_an_archetype(monkeypatch, capsys):
    _offline(monkeypatch)
    from ops import fleet as fleet_ops

    seen: dict = {}

    async def fake_create(name, **kw):
        seen.update(name=name, **kw)
        return {"agent": {"name": name, "id": "b-1", "port": 7902, "running": False}, "installed": []}

    monkeypatch.setattr(fleet_ops, "create", fake_create)
    assert cli.run_fleet_cli(["new", "beta", "--bundle", "https://github.com/x/y", "--no-start", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert seen == {"name": "beta", "bundle": "https://github.com/x/y", "port": None, "start": False, "inherit_config": True}
    assert body["mode"] == "offline" and body["results"][0]["ok"]
    assert cli.run_fleet_cli(["new", "beta", "--archetype", "pm"]) == 1
    assert "--archetype needs a running hub" in capsys.readouterr().err


def test_rm_needs_yes_off_a_terminal_and_a_409_is_retryable(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.run_fleet_cli(["rm", "alpha"]) == 1
    assert "--yes" in capsys.readouterr().err and not any(c[0] == "remove" for c in client.calls)
    assert cli.run_fleet_cli(["rm", "alpha", "--yes", "--purge", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert ("remove", "alpha", True) in client.calls and body["results"][0]["removed"] == ["workspace"]
    client.remove_status = 409
    assert cli.run_fleet_cli(["rm", "alpha", "--yes", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["results"][0]["retryable"] is True and "repeat to finish" in body["results"][0]["error"]
    client.remove_status = 400
    assert cli.run_fleet_cli(["rm", "alpha", "--yes"]) == 1
    assert "no such member" in capsys.readouterr().err
    # the interactive path: the typed name is the confirm
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "alpha")
    client.remove_status = None
    assert cli.run_fleet_cli(["rm", "alpha"]) == 0
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")
    assert cli.run_fleet_cli(["rm", "alpha"]) == 1
    assert "aborted" in capsys.readouterr().err


def test_rm_offline_runs_the_op_and_a_busy_workspace_is_retryable(monkeypatch, capsys):
    _offline(monkeypatch)
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    async def busy(ident, *, purge=False):
        raise manager.WorkspaceBusy("workspace survived")

    monkeypatch.setattr(fleet_ops, "remove", busy)
    assert cli.run_fleet_cli(["rm", "alpha", "--yes", "--purge", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["mode"] == "offline" and body["results"][0]["retryable"] is True

    async def ok(ident, *, purge=False):
        return {"name": ident, "removed": []}

    monkeypatch.setattr(fleet_ops, "remove", ok)
    assert cli.run_fleet_cli(["rm", "alpha", "--yes"]) == 0
    assert "removed (data kept) via disk (offline)" in capsys.readouterr().out


def test_rename_live_and_offline(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["rename", "alpha", "Alpha Prime", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert ("rename", "alpha", "Alpha Prime") in client.calls and body["results"][0]["new_name"] == "Alpha Prime" and body["results"][0]["id"] == "alpha-1"
    _offline(monkeypatch)
    from ops import fleet as fleet_ops

    async def fake(ident, new_name):
        return {"id": "alpha-1", "name": new_name}

    monkeypatch.setattr(fleet_ops, "rename", fake)
    assert cli.run_fleet_cli(["rename", "alpha", "Beta"]) == 0
    assert "renamed to Beta (id alpha-1 unchanged) via disk (offline)" in capsys.readouterr().out


def test_remote_add_edit_rm_live_with_the_bearer_from_stdin(monkeypatch, capsys):
    import io

    client = FakeClient()
    _live(monkeypatch, client)
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo.tail:7870", "--bearer-stdin", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert ("remote_add", "bo", "https://bo.tail:7870", "s3cret") in client.calls
    assert "s3cret" not in json.dumps(body) and body["results"][0]["reachable"] is False  # never echoed; unreachable is not an error
    assert cli.run_fleet_cli(["remote", "edit", "bo", "--url", "https://bo2.tail:7870"]) == 0
    assert ("remote_update", "bo", {"url": "https://bo2.tail:7870"}) in client.calls
    assert "reachable, v0.165.0" in capsys.readouterr().out
    assert cli.run_fleet_cli(["remote", "edit", "bo", "--clear-bearer"]) == 0
    assert ("remote_update", "bo", {"token": ""}) in client.calls
    assert cli.run_fleet_cli(["remote", "edit", "bo"]) == 1
    assert "nothing to change" in capsys.readouterr().err
    assert cli.run_fleet_cli(["remote", "rm", "bo", "--json"]) == 0
    assert ("remote_remove", "bo") in client.calls and json.loads(capsys.readouterr().out)["results"][0]["id"] == "bo"


def test_remote_offline_runs_the_ops(monkeypatch, capsys):
    _offline(monkeypatch)
    from ops import fleet as fleet_ops

    seen: list = []

    async def add(name, url, token=""):
        seen.append(("add", name, url, token))
        return {"agent": {"id": "r-1", "name": name, "url": url}, "reachable": False, "version": ""}

    async def update(ident, *, name=None, url=None, token=None):
        seen.append(("update", ident, name, url, token))
        return {"agent": {"id": ident, "name": name or "bo", "url": url or "u"}, "reachable": True, "version": "0.1"}

    async def remove(ident):
        seen.append(("remove", ident))
        return {"id": ident, "name": "bo", "removed": ["remote"]}

    monkeypatch.setattr(fleet_ops, "remotes_add", add)
    monkeypatch.setattr(fleet_ops, "remotes_update", update)
    monkeypatch.setattr(fleet_ops, "remotes_remove", remove)
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo:7870", "--bearer", "t"]) == 0
    assert cli.run_fleet_cli(["remote", "edit", "bo", "--name", "bob", "--clear-bearer"]) == 0
    capsys.readouterr()  # the human-mode lines of the first two
    assert cli.run_fleet_cli(["remote", "rm", "bo", "--json"]) == 0
    assert seen == [("add", "bo", "https://bo:7870", "t"), ("update", "bo", "bob", None, ""), ("remove", "bo")]
    assert json.loads(capsys.readouterr().out)["mode"] == "offline"


def test_order_live_and_offline(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    assert cli.run_fleet_cli(["order", "protoagent", "old-1", "protoEngineer-ba4c", "Cindi-9f49", "r-ava", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert ("set_order", ["protoagent", "old-1", "protoEngineer-ba4c", "Cindi-9f49", "r-ava"]) in client.calls and body["results"][0]["order"][1] == "old-1"
    _offline(monkeypatch)
    from ops import fleet as fleet_ops

    async def bad(order):
        raise cli.supervisor.FleetError("roster order is missing current member(s): main")

    monkeypatch.setattr(fleet_ops, "order", bad)
    assert cli.run_fleet_cli(["order", "alpha-1"]) == 1
    assert "missing current member" in capsys.readouterr().err


# ── round-1 review: credential and --json edges ──


def test_an_empty_bearer_never_clears_or_registers_and_flag_conflicts_are_refused(monkeypatch, capsys):
    import io

    client = FakeClient()
    _live(monkeypatch, client)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # an upstream `pass show` that failed
    assert cli.run_fleet_cli(["remote", "edit", "bo", "--bearer-stdin", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert "empty" in body["results"][0]["error"] and not any(c[0] == "remote_update" for c in client.calls)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo:7870", "--bearer-stdin"]) == 1
    assert "empty" in capsys.readouterr().err and not any(c[0] == "remote_add" for c in client.calls)
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo:7870", "--bearer", ""]) == 1
    assert cli.run_fleet_cli(["remote", "edit", "bo", "--bearer", "x", "--clear-bearer"]) == 1
    assert "ONE of" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("tok\n"))
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo:7870", "--bearer", "x", "--bearer-stdin"]) == 1
    assert not client.calls or not any(c[0] in ("remote_add", "remote_update") for c in client.calls)
    # a terminal never echoes the bearer: getpass
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "typed-secret")
    assert cli.run_fleet_cli(["remote", "add", "bo", "https://bo:7870", "--bearer-stdin"]) == 0
    assert ("remote_add", "bo", "https://bo:7870", "typed-secret") in client.calls
    # --archetype and --bundle together is ambiguous
    assert cli.run_fleet_cli(["new", "x", "--archetype", "pm", "--bundle", "https://g/x"]) == 1
    assert "not both" in capsys.readouterr().err and not any(c[0] == "create" for c in client.calls)


def test_rm_json_keeps_stdout_clean_and_tells_scripts_about_yes(monkeypatch, capsys):
    client = FakeClient()
    _live(monkeypatch, client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "alpha")
    assert cli.run_fleet_cli(["rm", "alpha", "--yes", "--json"]) == 0
    out, err = capsys.readouterr()
    json.loads(out)  # nothing but JSON on stdout
    assert cli.run_fleet_cli(["rm", "alpha", "--json"]) == 0  # the interactive confirm
    out, err = capsys.readouterr()
    json.loads(out)  # nothing but JSON on stdout
    assert "type the name to confirm" in err  # the prompt went to stderr
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.run_fleet_cli(["rm", "alpha", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["mode"] == "aborted" and "--yes" in body["results"][0]["error"]


# ── every hub on the box (#3472) ──


def _tree_rows():
    from pathlib import Path

    from deck.hubs import HubRow

    return [
        HubRow(name="studio", root=Path("/tmp/desktop"), url="http://127.0.0.1:7870", port=7870, presence="running", launcher="desktop app", version="0.165.0", pid=222, source="heartbeat", members=13, running=5, remotes=0, candidate=deckhub.HubCandidate("http://127.0.0.1:7870", "heartbeat")),
        HubRow(name="dev", root=Path("/tmp/dev"), url="http://127.0.0.1:7871", port=7871, presence="stopped", source="root", version="0.164.0", members=2, running=0, remotes=0),
        HubRow(name="ava", root=None, url="https://ava.tail:7870", port=7870, presence="unreachable", launcher="peer", source="peer", candidate=deckhub.HubCandidate("https://ava.tail:7870", "peer")),
    ]


def test_fleet_all_prints_the_hub_tree_and_json_carries_every_row(monkeypatch, capsys):

    probed: list = []
    monkeypatch.setattr("deck.discovery.enumerate_hubs", lambda *, peers=None: (probed.append(("peers", peers)) or _tree_rows()))

    def probe(row, *, token=None, insecure_http=False, roots=None):
        probed.append((row.name, token))
        if row.name == "ava":
            row.presence, row.note = "unauthorized", "answers, but every credential was refused — pass --token"
        return row

    monkeypatch.setattr("deck.discovery.probe", probe)
    monkeypatch.setattr(cli, "_discover_peers", lambda: [{"name": "ava", "url": "https://ava.tail:7870"}])
    assert cli.run_fleet_cli(["--all", "--json", "--token", "tok"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["mode"] == "hubs" and [h["name"] for h in body["hubs"]] == ["studio", "dev", "ava"]
    assert [h["presence"] for h in body["hubs"]] == ["running", "stopped", "unauthorized"]
    dev_root = str(Path("/tmp/dev"))  # rendered the platform's way (backslashes on Windows)
    assert body["hubs"][0]["launcher"] == "desktop app" and body["hubs"][0]["members"] == 13 and body["hubs"][1]["root"] == dev_root
    assert ("peers", [{"name": "ava", "url": "https://ava.tail:7870"}]) in probed and ("studio", "tok") in probed and ("ava", "tok") in probed
    assert not any(p[0] == "dev" for p in probed)  # a stopped hub has nothing to probe
    # the human table, off a terminal
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.run_fleet_cli(["--all"]) == 0
    out = capsys.readouterr().out
    assert "3 found · 1 running" in out and "unauthorized" in out and "pass --token" in out and dev_root in out
    # --offline: no peer scan, no probes
    probed.clear()
    assert cli.run_fleet_cli(["--all", "--offline", "--json"]) == 0
    assert probed == [("peers", [])] and json.loads(capsys.readouterr().out)["offline"] is True
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.run_fleet_cli(["--all", "--offline"]) == 0
    assert "--offline: peers not scanned, hubs not probed" in capsys.readouterr().out


def test_launch_hub_runs_protoagent_up_for_that_root_and_never_this_shells_scope(monkeypatch):
    from pathlib import Path

    from deck.hubs import HubRow

    seen: dict = {}

    class P:
        returncode = 0
        stdout = "protoagent: started on http://127.0.0.1:7871 (pid 5)"
        stderr = ""

    def fake_run(argv, *, env, capture_output, text, timeout):
        seen.update(argv=argv, env=env)
        return P()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "somewhere-else")
    monkeypatch.setenv("PROTOAGENT_HOME", "/nope")
    monkeypatch.setattr("deck.discovery.instance_roots", lambda: [])  # nothing on disk owns a port here (this box's roots would)
    held = {7870, 7871}  # the desktop hub and something else hold these
    monkeypatch.setattr(cli, "_port_free", lambda port: port not in held)
    row = HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=7871, presence="stopped", source="root")
    cli._launch_hub(row)  # its remembered port is held: the next free one in the range
    assert seen["argv"][-3:] == ["up", "--port", "7872"] and seen["env"]["PROTOAGENT_HOME"] == str(Path("/tmp/dev")) and "PROTOAGENT_INSTANCE" not in seen["env"]
    assert (row.port, row.url) == (7872, "http://127.0.0.1:7872")  # the row learned where it will answer
    held.discard(7871)
    row = HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=7871, presence="stopped", source="root")
    cli._launch_hub(row)
    assert seen["argv"][-3:] == ["up", "--port", "7871"]  # free again: the remembered port wins
    cli._launch_hub(HubRow(name="fresh", root=Path("/tmp/fresh"), url=None, port=None, presence="stopped", source="root"))
    assert seen["argv"][-3:] == ["up", "--port", "7871"]  # no memory: the first free one

    class Bad(P):
        returncode = 1
        stderr = "protoagent: port 7871 is held by a process `protoagent up` didn't start — free it, or pass --port\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Bad())
    with pytest.raises(RuntimeError, match="port 7871 is held"):
        cli._launch_hub(HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=7871, presence="stopped", source="root"))
    with pytest.raises(RuntimeError, match="instance root"):
        cli._launch_hub(HubRow(name="ava", root=None, url="https://ava:7870", port=7870, presence="unreachable", source="peer"))


def test_launch_hub_gives_the_desktop_root_its_box_root_and_the_port_probe_sees_a_wildcard_listener(monkeypatch, tmp_path):
    import socket
    from pathlib import Path

    from deck.hubs import HubRow

    seen: dict = {}

    class P:
        returncode = 0
        stdout = "started"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, *, env, capture_output, text, timeout: (seen.update(argv=argv, env=env) or P()))
    monkeypatch.setattr(cli, "_port_free", lambda port: True)
    desktop = tmp_path / "Application Support" / "studio.protolabs.protoagent"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(deckhub, "desktop_box_roots", lambda: [desktop])
    cli._launch_hub(HubRow(name="studio", root=desktop, url=None, port=7870, presence="stopped", source="root"))
    assert seen["env"]["PROTOAGENT_HOME"] == str(desktop) and seen["env"]["PROTOAGENT_BOX_ROOT"] == str(desktop)
    cli._launch_hub(HubRow(name="dev", root=Path("/tmp/dev"), url=None, port=7871, presence="stopped", source="root"))
    assert "PROTOAGENT_BOX_ROOT" not in seen["env"]  # a scoped instance keeps the machine's box root
    # a wildcard (0.0.0.0) listener holds the port even though a loopback bind would succeed
    monkeypatch.undo()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert cli._port_free(port) is False
    finally:
        srv.close()
    assert cli._port_free(port) is True


# ── round 2 (#3472): the launcher's port and box root, `--all` on a refusing hub, discovery knobs ──


class _Started:
    returncode = 0
    stdout = "protoagent: started"
    stderr = ""


def test_launch_hub_never_takes_a_port_another_hubs_member_owns_on_disk(tmp_path, monkeypatch):
    """Round 2: a stopped member's fixed port is free right now and binds — the launched hub
    took it, and that member died with EADDRINUSE at its next start."""
    from deck import hubs

    desktop = tmp_path / "desktop"
    ws = desktop / "workspaces"
    (ws / "m0").mkdir(parents=True)
    (ws / "m0" / "workspace.yaml").write_text("id: m0\nname: m0\nport: 7871\n")
    (ws / "fleet.json").write_text(json.dumps({"m0": {"pid": None, "port": 7871}}))  # stopped member on 7871
    dev = tmp_path / "dev"
    (dev / "workspaces").mkdir(parents=True)
    (dev / "workspaces" / "fleet.json").write_text("{}")
    (dev / "server.pid").write_text(json.dumps({"pid": 0, "port": 7873}))  # dev's own remembered port is never "taken" from itself
    seen: dict = {}
    monkeypatch.setattr("subprocess.run", lambda argv, *, env, capture_output, text, timeout: (seen.update(argv=argv, env=env) or _Started()))
    monkeypatch.setattr(cli, "_port_free", lambda port: port != 7870)  # the desktop hub holds 7870; 7871 binds (its member is stopped)
    monkeypatch.setattr(deckhub, "desktop_box_roots", lambda: [desktop])
    monkeypatch.setattr("deck.discovery.instance_roots", lambda: [desktop, dev])
    cli._launch_hub(hubs.HubRow(name="dev", root=dev, url=None, port=None, presence="stopped", source="root"))
    assert seen["argv"][-3:] == ["up", "--port", "7872"]
    cli._launch_hub(hubs.HubRow(name="dev", root=dev, url=None, port=7871, presence="stopped", source="root"))
    assert seen["argv"][-3:] == ["up", "--port", "7872"]  # a remembered port that a member owns is not honoured either
    cli._launch_hub(hubs.HubRow(name="dev", root=dev, url=None, port=7873, presence="stopped", source="root"))
    assert seen["argv"][-3:] == ["up", "--port", "7873"]  # its own server.pid port is its to keep


def test_launch_hub_keeps_the_box_root_of_a_scoped_instance_under_a_custom_box(tmp_path, monkeypatch):
    """Round 2: the child got only PROTOAGENT_HOME, so a scoped instance under this shell's
    PROTOAGENT_BOX_ROOT resolved its box to ~/.protoagent — wrong Host layer, commons,
    credential store, and a heartbeat under the wrong .instances/."""
    import subprocess
    import sys
    from pathlib import Path

    from deck import hubs

    box = tmp_path / "box"
    home = tmp_path / "home"
    home.mkdir()
    dev = box / "dev"
    (dev / "workspaces").mkdir(parents=True)
    (dev / "workspaces" / "fleet.json").write_text("{}")
    monkeypatch.setattr(deckhub, "known_box_roots", lambda: [box, home])
    monkeypatch.setattr(deckhub, "data_home", lambda: home)
    monkeypatch.setattr(deckhub, "desktop_box_roots", lambda: [tmp_path / "desktop-absent"])
    monkeypatch.setattr("deck.discovery.instance_roots", lambda: [dev])
    seen: dict = {}
    real_run = subprocess.run
    monkeypatch.setattr("subprocess.run", lambda argv, *, env, capture_output, text, timeout: (seen.update(argv=argv, env=env) or _Started()))
    monkeypatch.setattr(cli, "_port_free", lambda port: True)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(box))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")
    cli._launch_hub(hubs.HubRow(name="dev", root=dev, url=None, port=None, presence="stopped", source="root"))
    env = dict(seen["env"])
    assert env["PROTOAGENT_HOME"] == str(dev) and env["PROTOAGENT_BOX_ROOT"] == str(box) and "PROTOAGENT_INSTANCE" not in env
    # what the CHILD resolves from that env — a real interpreter, this checkout, no server
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repo)
    out = real_run([sys.executable, "-c", "from infra.paths import instance_paths as p; print(p().box_root); print(p().instance_root)"], env=env, capture_output=True, text=True, cwd=str(repo), timeout=60)
    child_box, child_root = out.stdout.strip().splitlines()[-2:]
    assert (Path(child_box), Path(child_root)) == (box, dev), out.stderr
    # a scoped instance under the plain data home gets no box root: the default IS that home
    seen.clear()
    cli._launch_hub(hubs.HubRow(name="dev2", root=home / "dev2", url=None, port=None, presence="stopped", source="root"))
    assert "PROTOAGENT_BOX_ROOT" not in seen["env"]


def test_fleet_all_on_a_tty_opens_the_tree_even_when_a_hub_refuses_every_credential(monkeypatch, capsys):
    """Round 2: `--all` is documented "never reads one hub's fleet", but the deck opened the
    default hub first and a rejected credential was an error — the tree, the one view that
    shows that hub as `unauthorized`, never opened."""
    import importlib

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _offline(monkeypatch, unauthorized=["http://127.0.0.1:7870"])
    opened: dict = {}

    class FakeApp:
        @staticmethod
        def run(backend, **kw):
            opened.update(kw, mode=backend.mode, label=backend.snapshot().label, lifecycle=getattr(backend, "lifecycle", True))
            return 0

    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **kw: FakeApp if name == "deck.app" else real(name, *a, **kw))
    rc = cli.run_fleet_cli(["--all"])
    assert rc == 0 and opened.get("start_on_hubs") is True and opened["mode"] == "offline" and "rejected every credential" in opened["label"], capsys.readouterr().err
    assert opened["lifecycle"] is False and "start/stop refused" in opened["label"]  # a hub answered: nothing is driven from disk beside it
    opened.clear()
    _offline(monkeypatch)  # nothing answered at all: the disk view keeps start/stop
    assert cli.run_fleet_cli(["--all"]) == 0 and opened["lifecycle"] is True
    _offline(monkeypatch, unauthorized=["http://127.0.0.1:7870"])
    # without --all the rule stands: a hub that answered but refused is an error, not a fallback
    opened.clear()
    assert cli.run_fleet_cli([]) == 1 and not opened
    # --offline reaches the deck as its own flag: the tree then lists disk and probes nothing
    opened.clear()
    assert cli.run_fleet_cli(["--all", "--offline"]) == 0 and opened.get("offline") is True and opened.get("peers") is None


def test_discover_peers_honours_the_boxs_discovery_knobs(tmp_path, monkeypatch):
    """Round 2: `discovery` reads `fleet.discovery.*` from the live server's STATE, which a
    CLI process has none of — so `mdns: true` never opened the LAN channel and the port
    range was always the default, whatever the docs promised."""
    import os
    from pathlib import Path

    from graph.fleet import discovery

    # the Host layer file the cascade reads (conftest points PROTOAGENT_HOST_CONFIG at a tmp one)
    Path(os.environ["PROTOAGENT_HOST_CONFIG"]).write_text("fleet:\n  discovery:\n    mdns: true\n    port_min: 7870\n    port_max: 7872\n", encoding="utf-8")
    calls: dict = {}

    async def scan_local(port_range, skip):
        calls["local_range"] = port_range
        return []

    async def scan_tailnet(port_range, known):
        calls["tailnet_range"] = port_range
        return []

    monkeypatch.setattr(discovery, "_scan_local", scan_local)
    monkeypatch.setattr(discovery, "_scan_tailnet", scan_tailnet)
    monkeypatch.setattr(discovery, "_browse_mdns", lambda timeout: calls.setdefault("mdns", True) and [])
    monkeypatch.setattr(discovery, "_local_ip", lambda: "127.0.0.1")
    assert cli._discover_peers() == []
    assert discovery._cfg() is None  # no live config in a CLI process: the knobs came from the cascade
    assert calls.get("mdns") is True and calls.get("local_range") == (7870, 7872), calls


def test_fleet_all_strips_control_characters_from_what_a_peer_or_hub_said(monkeypatch, capsys):
    """CodeRabbit (S5): a discovered peer controls its name and a hub its version/note; both
    flowed into print() — a newline breaks the row, an ESC or OSC sequence can rewrite the
    screen. The table strips C0/C1; `--json` keeps the raw value."""
    from deck import hubs

    evil = "evil\x1b[2J\x07\nhub"
    row = hubs.HubRow(name=evil, root=None, url="https://ava.tail:7870", port=7870, presence="unauthorized", launcher="peer", source="peer", version="1\x9b0", note="refused\x1b]0;pwned\x07")
    monkeypatch.setattr("deck.discovery.enumerate_hubs", lambda *, peers=None: [row])
    monkeypatch.setattr("deck.discovery.probe", lambda r, **kw: r)
    monkeypatch.setattr("deck.discovery.reconcile", lambda rows: rows)
    monkeypatch.setattr(cli, "_discover_peers", lambda: [])
    assert cli.run_fleet_cli(["--all"]) == 0
    out = capsys.readouterr().out
    assert "evil[2Jhub" in out and "v10" in out and "pwned" in out
    assert not any(ord(ch) < 32 and ch != "\n" or 0x7F <= ord(ch) <= 0x9F for ch in out)
    assert cli.run_fleet_cli(["--all", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["hubs"][0]["name"] == evil and body["hubs"][0]["version"] == "1\x9b0"  # raw, for scripts

def test_the_non_interactive_verbs_never_load_textual(tmp_path):
    """S6 (#3473): `fleet --all --json` reached the hub enumerator through `deck.hubs`, whose
    module top imports the Textual screen — a frozen build without Textual would have died
    with ModuleNotFoundError there instead of the documented hint, and every scripted
    `--all --json` paid the import. The enumerator lives in `deck.discovery` now."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo), PROTOAGENT_HOME=str(tmp_path / "inst"), PROTOAGENT_BOX_ROOT=str(tmp_path / "box"))
    probe = "import sys; from graph.fleet import cli\ntry:\n    rc = cli.run_fleet_cli({argv!r})\nexcept SystemExit as e:\n    rc = e.code\nprint('textual' in sys.modules, 'deck.hubs' in sys.modules, rc)"
    for argv in (["--all", "--offline", "--json"], ["ls", "--offline", "--json"], ["--help"]):
        out = subprocess.run([sys.executable, "-c", probe.format(argv=argv)], env=env, capture_output=True, text=True, cwd=str(repo), timeout=120)
        last = (out.stdout.strip().splitlines() or [""])[-1]
        assert last.startswith("False False"), (argv, last, out.stderr[-500:])


def test_launch_hub_never_takes_a_stopped_members_port_that_only_its_workspace_yaml_records(tmp_path, monkeypatch):
    """Integrated test on the epic (live): the dev hub came up on :7871, which the desktop's
    STOPPED killteamCoach member records in its workspace.yaml — `fleet.json` lists only the
    members the supervisor started, so the check that read it alone passed falsely."""
    from deck import discovery

    desktop = tmp_path / "desktop"
    (desktop / "workspaces" / "killteam-0416").mkdir(parents=True)
    (desktop / "workspaces" / "killteam-0416" / "workspace.yaml").write_text("id: killteam-0416\nname: killteam\nport: 7871\n")
    (desktop / "workspaces" / "roxy-e815").mkdir(parents=True)
    (desktop / "workspaces" / "roxy-e815" / "workspace.yaml").write_text("id: roxy-e815\nname: roxy\nport: 7872\n")
    (desktop / "workspaces" / "fleet.json").write_text(json.dumps({"roxy-e815": {"pid": 7, "port": 7872}}))  # only the RUNNING member
    dev = tmp_path / "dev"
    (dev / "workspaces").mkdir(parents=True)
    (dev / "workspaces" / "fleet.json").write_text("{}")
    members, _ = discovery._ports_on_disk([desktop, dev], records=True)
    assert {7871, 7872} <= set(members)  # the stopped member's port is known from its record
    assert set(discovery._ports_on_disk([desktop, dev])[0]) == {7872}  # the tree's listener skip: started members only
    seen: dict = {}
    monkeypatch.setattr("subprocess.run", lambda argv, *, env, capture_output, text, timeout: (seen.update(argv=argv) or _Started()))
    monkeypatch.setattr(cli, "_port_free", lambda port: port != 7870)  # the desktop hub holds 7870; 7871 and 7872 bind (7871's member is stopped)
    monkeypatch.setattr(deckhub, "desktop_box_roots", lambda: [desktop])
    monkeypatch.setattr("deck.discovery.instance_roots", lambda: [desktop, dev])
    cli._launch_hub(discovery.HubRow(name="dev", root=dev, url=None, port=None, presence="stopped", source="root"))
    assert seen["argv"][-3:] == ["up", "--port", "7873"]
