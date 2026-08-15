"""``timed_lifecycle_phase`` (#2675) — per-plugin lifecycle stage timing (load /
config / registration), instrumentation point #3 from #2245. Same
``time.monotonic()`` idiom as ``AuditMiddleware``'s tool-call timing and
``server.agent_init._timed_boot_phase`` (#2674), applied per plugin, with a
>500ms WARNING naming the slow plugin and stage."""

from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest

import graph.plugins.host as plugin_host
from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins import pconfig
from graph.plugins.host import timed_lifecycle_phase
from graph.plugins.loader import load_plugins
from observability import metrics

_PLUGIN_BODY = '''
from langchain_core.tools import tool

@tool
async def lifecycle_probe(x: str = "") -> str:
    """example"""
    return x

def register(registry):
    registry.register_tool(lifecycle_probe)
'''


def _make_plugin(root: Path, pid: str, *, body: str | None = None, manifest_extra: str = "") -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: true\n{manifest_extra}",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(_PLUGIN_BODY if body is None else body, encoding="utf-8")
    return d


def _scripted_clock(monkeypatch, ticks: list[float]) -> None:
    """Deterministic clock, bound into host.py's namespace only (the sibling
    test_boot_phase_timing.py scripts the shared ``time`` module and explains the
    Windows-granularity flake a real sleep causes). Each ``time.monotonic()`` read
    inside the helper consumes one tick, then sticks on the last."""
    seq = iter(ticks)
    last = ticks[-1]

    def fake_monotonic() -> float:
        nonlocal last
        try:
            last = next(seq)
        except StopIteration:
            pass
        return last

    monkeypatch.setattr(plugin_host, "time", types.SimpleNamespace(monotonic=fake_monotonic))


def _capture_records(monkeypatch) -> list[tuple[str, str, float]]:
    calls: list[tuple[str, str, float]] = []
    monkeypatch.setattr(
        metrics,
        "record_plugin_lifecycle",
        lambda plugin, phase, duration_s: calls.append((plugin, phase, duration_s)),
    )
    return calls


def test_timed_lifecycle_phase_records_the_measured_elapsed_time(monkeypatch):
    # Scripted clock pins the recorded value to the exact delta between the
    # helper's two reads — a hardcoded 0.0 or a swapped subtraction fails here.
    _scripted_clock(monkeypatch, [100.0, 100.25])
    calls = _capture_records(monkeypatch)
    with timed_lifecycle_phase("p1", "load"):
        pass
    assert calls == [("p1", "load", pytest.approx(0.25))]


def test_timed_lifecycle_phase_records_even_when_the_stage_raises(monkeypatch):
    # A stage that raises (a bad plugin) must still be timed — that is exactly
    # the plugin worth diagnosing, and the loader's except-and-continue means
    # nothing downstream would otherwise ever see its duration.
    calls = _capture_records(monkeypatch)
    with pytest.raises(RuntimeError):
        with timed_lifecycle_phase("badp", "registration"):
            raise RuntimeError("boom")
    assert len(calls) == 1
    assert calls[0][0] == "badp" and calls[0][1] == "registration"
    assert calls[0][2] >= 0


def test_slow_stage_logs_a_warning_naming_plugin_and_stage(monkeypatch, caplog):
    _scripted_clock(monkeypatch, [10.0, 10.75])
    _capture_records(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        with timed_lifecycle_phase("p-slow", "load"):
            pass
    assert "p-slow" in caplog.text
    assert "load stage took 0.75s" in caplog.text


def test_fast_and_exactly_threshold_stages_do_not_warn(monkeypatch, caplog):
    # A 100ms run and an exactly-500ms run: the outlier gate is strictly
    # greater-than the threshold, so neither may warn.
    _scripted_clock(monkeypatch, [10.0, 10.1, 20.0, 20.0 + plugin_host.SLOW_LIFECYCLE_THRESHOLD_S])
    _capture_records(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        with timed_lifecycle_phase("p-fast", "config"):
            pass
        with timed_lifecycle_phase("p-borderline", "config"):
            pass
    assert "stage took" not in caplog.text


def test_load_plugins_times_all_three_stages_per_plugin(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    _make_plugin(root, "tlp")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    calls = _capture_records(monkeypatch)

    res = load_plugins(LangGraphConfig())

    meta = {m["id"]: m for m in res.meta}
    assert meta["tlp"]["loaded"] is True
    assert [(plugin, phase) for plugin, phase, _ in calls] == [
        ("tlp", "load"),
        ("tlp", "config"),
        ("tlp", "registration"),
    ]
    assert all(duration >= 0 for _, _, duration in calls)


def test_failed_plugin_import_still_records_the_load_stage(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    _make_plugin(root, "boomp", body="raise RuntimeError('boom at import')\n")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    calls = _capture_records(monkeypatch)

    res = load_plugins(LangGraphConfig())

    meta = {m["id"]: m for m in res.meta}
    assert meta["boomp"]["loaded"] is False
    assert "boom at import" in meta["boomp"]["error"]
    # The import blew up, so only the load stage ran — and it was still timed.
    assert [(plugin, phase) for plugin, phase, _ in calls] == [("boomp", "load")]


def test_discover_plugin_config_times_the_config_stage(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    _make_plugin(root, "cfp", manifest_extra="config:\n  greeting: hello\n")
    calls = _capture_records(monkeypatch)

    schemas = pconfig.discover_plugin_config([root], {"cfp"})

    assert [s.plugin_id for s in schemas] == ["cfp"]
    assert [(plugin, phase) for plugin, phase, _ in calls] == [("cfp", "config")]


def test_record_plugin_lifecycle_noops_when_metrics_disabled(monkeypatch):
    monkeypatch.setattr(metrics, "_enabled", False)
    metrics.record_plugin_lifecycle("p", "load", 0.1)  # must not raise


def test_record_plugin_lifecycle_labels_plugin_and_phase(monkeypatch):
    # Pin the label names the dashboards will query — {plugin=...,phase=...}.
    class FakeHist:
        def __init__(self):
            self.observed: list[tuple[dict, float]] = []
            self._labels: dict = {}

        def labels(self, **kw):
            self._labels = kw
            return self

        def observe(self, value):
            self.observed.append((dict(self._labels), value))

    fake = FakeHist()
    monkeypatch.setattr(metrics, "_enabled", True)
    monkeypatch.setattr(metrics, "_plugin_lifecycle_latency", fake)
    metrics.record_plugin_lifecycle("notes", "registration", 0.02)
    assert fake.observed == [({"plugin": "notes", "phase": "registration"}, 0.02)]
