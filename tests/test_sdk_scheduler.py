"""sdk.schedule_recurring / cancel_scheduled / cancel_plugin_jobs (#1642) — plugin-owned
recurring jobs with `plugin:<plugin_id>:<job_id>` ownership tagging, plus the lifecycle
hygiene hooks: the loader sweeps a disabled plugin's jobs on (re)load and the installer
sweeps on uninstall, so no orphan cadence outlives its plugin.

The ADR 0004 `agent_name` scoping is orthogonal and untouched — the LocalScheduler
round-trip test exercises the real backend to prove the namespaced ids ride the normal
per-instance jobs.db path.
"""

from __future__ import annotations

import pytest

from graph import sdk
from runtime.state import STATE
from scheduler.interface import is_cron


class _Job:
    def __init__(self, jid, schedule="", prompt="", context_id=None, timezone=None):
        self.id = jid
        self.schedule = schedule
        self.prompt = prompt
        self.context_id = context_id
        self.timezone = timezone
        self.next_fire = "2026-07-03T09:00:00+00:00"


class _Scheduler:
    """Protocol-shaped fake — add/cancel/list against an in-memory dict."""

    def __init__(self, seed: tuple[str, ...] = ()):
        self.jobs: dict[str, _Job] = {jid: _Job(jid) for jid in seed}
        self.cancelled: list[str] = []

    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        jid = job_id or "auto-1"
        if jid in self.jobs:
            raise ValueError(f"job id {jid!r} already exists")
        job = _Job(jid, schedule, prompt, context_id, timezone)
        self.jobs[jid] = job
        return job

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return self.jobs.pop(job_id, None) is not None

    def list_jobs(self):
        return list(self.jobs.values())


@pytest.fixture
def sched(monkeypatch):
    s = _Scheduler()
    monkeypatch.setattr(STATE, "scheduler", s)
    return s


# --- schedule_recurring ------------------------------------------------------


def test_schedule_recurring_namespaces_and_defaults_to_activity(sched):
    res = sdk.schedule_recurring(
        "Run the strategist OODA tick.", "0 9 * * *", plugin_id="spacetraders", job_id="strategist-tick"
    )
    assert res["ok"] is True
    assert res["job_id"] == "plugin:spacetraders:strategist-tick"  # ownership tag on the id
    job = sched.jobs[res["job_id"]]
    assert is_cron(job.schedule)  # recurring, not a one-shot ISO
    assert job.context_id is None  # None → the durable Activity thread
    assert res["next_fire"]


def test_schedule_recurring_targets_a_session_and_timezone(sched):
    res = sdk.schedule_recurring(
        "tick", "*/5 * * * *", plugin_id="st", job_id="t", session="sess-7", timezone="America/Chicago"
    )
    job = sched.jobs[res["job_id"]]
    assert job.context_id == "sess-7"
    assert job.timezone == "America/Chicago"


def test_schedule_recurring_rejects_non_cron(sched):
    # One-shot ISO fire times belong to run_in_session; garbage is refused too.
    for schedule in ("2026-07-03T09:00:00+00:00", "tomorrow", ""):
        res = sdk.schedule_recurring("p", schedule, plugin_id="st", job_id="t")
        assert res["ok"] is False and "cron" in res["message"]
    assert sched.jobs == {}  # nothing enqueued


def test_schedule_recurring_replaces_by_id(sched):
    sdk.schedule_recurring("p", "0 9 * * *", plugin_id="st", job_id="tick")
    res = sdk.schedule_recurring("p", "0 */2 * * *", plugin_id="st", job_id="tick")  # cadence knob changed
    assert res["ok"] is True
    assert "plugin:st:tick" in sched.cancelled  # idempotent: dropped before re-adding
    assert len(sched.jobs) == 1
    assert sched.jobs["plugin:st:tick"].schedule == "0 */2 * * *"


def test_schedule_recurring_validates_inputs(monkeypatch):
    monkeypatch.setattr(STATE, "scheduler", None)
    assert not sdk.schedule_recurring("p", "0 9 * * *", plugin_id="st", job_id="t")["ok"]  # no scheduler
    s = _Scheduler()
    monkeypatch.setattr(STATE, "scheduler", s)
    assert not sdk.schedule_recurring("p", "0 9 * * *", plugin_id="", job_id="t")["ok"]  # no plugin_id
    # ':' would let one plugin's id shadow another's namespace (plugin "a" sweeps "a:b"'s jobs).
    assert not sdk.schedule_recurring("p", "0 9 * * *", plugin_id="a:b", job_id="t")["ok"]
    assert not sdk.schedule_recurring("p", "0 9 * * *", plugin_id="st", job_id=" ")["ok"]  # no job_id
    assert not sdk.schedule_recurring(" ", "0 9 * * *", plugin_id="st", job_id="t")["ok"]  # no prompt
    assert s.jobs == {}  # nothing enqueued on bad input


# --- cancel_scheduled --------------------------------------------------------


def test_cancel_scheduled_round_trip(sched):
    sdk.schedule_recurring("p", "0 9 * * *", plugin_id="st", job_id="tick")
    assert sdk.cancel_scheduled("tick", plugin_id="st") is True  # plugin-local id, namespaced inside
    assert sched.jobs == {}
    assert sdk.cancel_scheduled("tick", plugin_id="st") is False  # already gone


def test_cancel_scheduled_unavailable_or_bad_input(monkeypatch):
    monkeypatch.setattr(STATE, "scheduler", None)
    assert sdk.cancel_scheduled("t", plugin_id="st") is False
    monkeypatch.setattr(STATE, "scheduler", _Scheduler())
    assert sdk.cancel_scheduled("", plugin_id="st") is False
    assert sdk.cancel_scheduled("t", plugin_id="") is False
    # Same ':' guard as schedule_recurring: plugin "a:b" can't reach into "a"'s namespace
    # (plugin "a" may legitimately hold a job whose plugin-local id contains ':').
    monkeypatch.setattr(STATE, "scheduler", _Scheduler(seed=("plugin:a:b:t",)))
    assert sdk.cancel_scheduled("t", plugin_id="a:b") is False


# --- cancel_plugin_jobs ------------------------------------------------------


def test_cancel_plugin_jobs_cancels_only_the_plugins_jobs(monkeypatch):
    s = _Scheduler(
        seed=(
            "plugin:st:tick",
            "plugin:st:watchdog",
            "plugin:st2:tick",  # prefix-adjacent plugin id must NOT match
            "plugin:other:tick",
            "operator-manual-job",
        )
    )
    monkeypatch.setattr(STATE, "scheduler", s)
    assert sdk.cancel_plugin_jobs("st") == 2
    assert set(s.jobs) == {"plugin:st2:tick", "plugin:other:tick", "operator-manual-job"}
    assert sdk.cancel_plugin_jobs("st") == 0  # nothing left to cancel


def test_cancel_plugin_jobs_unavailable_or_blank(monkeypatch):
    monkeypatch.setattr(STATE, "scheduler", None)
    assert sdk.cancel_plugin_jobs("st") == 0
    monkeypatch.setattr(STATE, "scheduler", _Scheduler(seed=("plugin:st:tick",)))
    assert sdk.cancel_plugin_jobs("") == 0  # a blank id must not sweep anything


# --- real LocalScheduler round-trip (agent_name scoping stays underneath) ----


def test_local_scheduler_round_trip(tmp_path, monkeypatch):
    from scheduler.local import LocalScheduler

    real = LocalScheduler(
        agent_name="sdk-test-agent",
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path,
    )
    monkeypatch.setattr(STATE, "scheduler", real)
    res = sdk.schedule_recurring("daily tick", "0 9 * * *", plugin_id="st", job_id="tick")
    assert res["ok"] is True and res["next_fire"]
    jobs = real.list_jobs()
    assert [j.id for j in jobs] == ["plugin:st:tick"]
    assert jobs[0].agent_name == "sdk-test-agent"  # ADR 0004 instance scoping untouched
    assert sdk.cancel_scheduled("tick", plugin_id="st") is True
    assert real.list_jobs() == []


# --- loader hygiene: disabled plugin's jobs are swept on (re)load ------------


def _make_plugin_dir(root, pid, *, enabled):
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid}\nversion: 0.1.0\nenabled: {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text("def register(registry):\n    pass\n", encoding="utf-8")
    return d


def test_loader_sweeps_disabled_plugins_jobs(tmp_path, monkeypatch):
    from graph.config import LangGraphConfig
    from graph.plugins import loader as plugin_loader
    from graph.plugins.loader import load_plugins

    root = tmp_path / "plugins"
    _make_plugin_dir(root, "offplug", enabled=False)
    _make_plugin_dir(root, "onplug", enabled=True)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    s = _Scheduler(seed=("plugin:offplug:tick", "plugin:onplug:tick", "operator-manual-job"))
    monkeypatch.setattr(STATE, "scheduler", s)

    load_plugins(LangGraphConfig())

    # The disabled plugin's cadence is gone; the enabled plugin's and the operator's survive.
    assert set(s.jobs) == {"plugin:onplug:tick", "operator-manual-job"}


def test_loader_sweep_noops_without_scheduler(tmp_path, monkeypatch):
    # Pre-setup / boot-ordering safety: no scheduler wired → the sweep must not blow up a load.
    from graph.config import LangGraphConfig
    from graph.plugins import loader as plugin_loader
    from graph.plugins.loader import load_plugins

    root = tmp_path / "plugins"
    _make_plugin_dir(root, "offplug", enabled=False)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    monkeypatch.setattr(STATE, "scheduler", None)
    res = load_plugins(LangGraphConfig())
    assert res.meta[0]["enabled"] is False  # load completed normally


# --- installer hygiene: uninstall cancels the plugin's jobs ------------------


def test_uninstall_cancels_plugin_jobs(tmp_path, monkeypatch):
    from graph.plugins import installer

    # Point the installer at a temp area (mirrors test_plugin_installer's env fixture);
    # a hand-placed (untracked) plugin dir is enough — uninstall removes it as "code".
    import graph.config_io as cio

    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    _make_plugin_dir(installer.live_plugins_dir(), "demo_sweep", enabled=True)

    s = _Scheduler(seed=("plugin:demo_sweep:tick", "plugin:other:tick"))
    monkeypatch.setattr(STATE, "scheduler", s)

    rep = installer.uninstall("demo_sweep")
    assert rep["jobs_cancelled"] == 1 and "jobs" in rep["removed"]
    assert set(s.jobs) == {"plugin:other:tick"}  # ONLY the uninstalled plugin's jobs


def test_uninstall_reports_zero_jobs_without_scheduler(tmp_path, monkeypatch):
    # CLI-process posture: no live scheduler → uninstall still succeeds, jobs_cancelled=0.
    from graph.plugins import installer

    import graph.config_io as cio

    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    _make_plugin_dir(installer.live_plugins_dir(), "demo_sweep", enabled=True)
    monkeypatch.setattr(STATE, "scheduler", None)

    rep = installer.uninstall("demo_sweep")
    assert rep["jobs_cancelled"] == 0 and "jobs" not in rep["removed"]


# --- surface ------------------------------------------------------------------


def test_sdk_module_exposes_recurring_surface():
    assert callable(sdk.schedule_recurring)
    assert callable(sdk.cancel_scheduled)
    assert callable(sdk.cancel_plugin_jobs)
    assert sdk.plugin_job_prefix("st") == "plugin:st:"
