"""Plugin telemetry + decision-log kit (graph.telemetry).

Generalizes the SpaceTraders observability surface: a decision-log ring buffer, the standard
telemetry envelope, and a themed HTML panel. Pure stdlib — tested directly.
"""

from __future__ import annotations

from graph.sdk import DecisionLog as SdkDecisionLog  # re-exported on the plugin SDK surface
from graph.telemetry import DecisionLog, render_html, telemetry


def test_exported_on_the_sdk():
    assert SdkDecisionLog is DecisionLog


# ── DecisionLog ──────────────────────────────────────────────────────────────────────
def test_decision_log_records_action_detail_and_extra_fields():
    log = DecisionLog()
    e = log.record("tune", "min_margin: 30 → 15", reason="cr/hr falling")
    assert e == {"action": "tune", "detail": "min_margin: 30 → 15", "reason": "cr/hr falling"}
    assert log.entries() == [e] and len(log) == 1


def test_decision_log_is_capped_newest_kept():
    log = DecisionLog(cap=3)
    for i in range(5):
        log.record("tune", f"k{i}")
    details = [e["detail"] for e in log.entries()]
    assert details == ["k2", "k3", "k4"]  # oldest two fell off, newest last


def test_decision_log_entries_n_and_clear():
    log = DecisionLog()
    for i in range(4):
        log.record("a", str(i))
    assert [e["detail"] for e in log.entries(2)] == ["2", "3"]
    log.clear()
    assert log.entries() == [] and len(log) == 0


# ── telemetry envelope ───────────────────────────────────────────────────────────────
def test_telemetry_envelope_has_the_standard_shape():
    env = telemetry(status="running", metrics={"credits": 1000}, hints=["reinvest"])
    assert env["status"] == "running"
    assert env["metrics"] == {"credits": 1000}
    assert env["hints"] == ["reinvest"]
    assert env["decisions"] == [] and env["sections"] == []


def test_telemetry_accepts_a_decisionlog_or_a_list():
    log = DecisionLog()
    log.record("strategy", "→ trade-max")
    assert telemetry(decisions=log)["decisions"] == [{"action": "strategy", "detail": "→ trade-max"}]
    raw = [{"action": "tune", "detail": "x"}]
    assert telemetry(decisions=raw)["decisions"] == raw


def test_telemetry_passes_extra_keys_through():
    env = telemetry(status="ok", credits=1234, per_hour=42)
    assert env["credits"] == 1234 and env["per_hour"] == 42


# ── render_html ──────────────────────────────────────────────────────────────────────
def test_render_html_includes_metrics_decisions_and_hints():
    log = DecisionLog()
    log.record("tune", "min_margin 30→15")
    env = telemetry(status="running · 1,000,000 cr", metrics={"credits": 1_000_000},
                    hints=["idle capital — reinvest"], decisions=log)
    out = render_html(env, title="Fleet")
    assert "<section" in out and "pl-tele" in out
    assert "Fleet" in out and "running · 1,000,000 cr" in out
    assert "1,000,000" in out          # int metric comma-formatted
    assert "min_margin 30→15" in out and "idle capital — reinvest" in out
    assert "--pl-color-fg" in out      # themed via DS tokens (with fallbacks)


def test_render_html_renders_section_tables():
    env = telemetry(sections=[{"title": "Fleet", "columns": ["ship", "role"],
                               "rows": [["DRONE-1", "miner"], ["HAULER-1", "trader"]]}])
    out = render_html(env)
    assert "<th>ship</th>" in out and "<td>DRONE-1</td>" in out and "<td>miner</td>" in out


def test_render_html_escapes_values():
    env = telemetry(status="<script>alert(1)</script>", hints=["a & b <c>"])
    out = render_html(env)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out and "a &amp; b &lt;c&gt;" in out


def test_render_html_handles_empty_envelope():
    out = render_html(telemetry())
    assert "<section" in out and "</section>" in out  # no metrics/decisions/hints → still valid


def test_decisions_render_newest_first():
    log = DecisionLog()
    log.record("a", "first")
    log.record("b", "second")
    out = render_html(telemetry(decisions=log))
    assert out.index("second") < out.index("first")  # newest at the top of the table
