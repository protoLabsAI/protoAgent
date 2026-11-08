"""ADR 0128 S0 (#4875): the per-call usage fields, the empty-completions counter, and
the host-free measure_output_budget.py reproducing the audit metrics on both input shapes.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from graph.agent import _extract_subagent_usage
from observability import metrics

_SCRIPT = Path(__file__).parent.parent / "scripts" / "measure_output_budget.py"
_SPEC = importlib.util.spec_from_file_location("measure_output_budget_under_test", _SCRIPT)
measure = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(measure)


# --------------------------------------------------------------------------- #
# 1. The four descriptive fields on each usage row.
# --------------------------------------------------------------------------- #
def _row(msg: AIMessage) -> dict:
    rows = _extract_subagent_usage([msg], subagent_type="review-finder", fallback_model="smart")
    assert len(rows) == 1
    return rows[0]


def test_reasoning_and_cap_message_gives_all_four_fields():
    msg = AIMessage(
        content=[{"type": "thinking", "thinking": "x" * 5000}, {"type": "text", "text": "Answer"}],
        response_metadata={"model_name": "smart", "finish_reason": "length"},
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 9000,
            "total_tokens": 9100,
            "output_token_details": {"reasoning": 1234},
        },
    )
    row = _row(msg)
    assert row["finish_reason"] == "length"
    assert row["text_chars"] == len("Answer")  # reasoning block excluded
    assert row["has_tool_calls"] is False
    assert row["reasoning_tokens"] == 1234
    # Additive only — the existing shape is untouched.
    assert row["output_tokens"] == 9000 and row["subagent_type"] == "review-finder"


def test_tool_call_turn_and_stop_reason_alias():
    msg = AIMessage(
        content="",
        response_metadata={"model_name": "smart", "stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "t1", "type": "tool_call"}],
    )
    row = _row(msg)
    assert row["finish_reason"] == "tool_use"  # Anthropic stop_reason alias
    assert row["has_tool_calls"] is True
    assert row["text_chars"] == 0
    assert row["reasoning_tokens"] is None


def test_no_details_gives_none_and_does_not_crash():
    msg = AIMessage(
        content=[{"type": "reasoning", "reasoning": "only thinking, no visible text"}],
        response_metadata={"model_name": "smart"},
        usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    )
    row = _row(msg)
    assert row["finish_reason"] is None
    assert row["reasoning_tokens"] is None
    assert row["text_chars"] == 0  # reasoning-only -> no visible text
    assert row["has_tool_calls"] is False


def test_extraction_never_raises_on_garbage():
    # A non-AIMessage and an AIMessage without usage_metadata are both skipped, not fatal.
    rows = _extract_subagent_usage(
        ["not a message", AIMessage(content="no usage")],
        subagent_type="x",
    )
    assert rows == []


# --------------------------------------------------------------------------- #
# 2. The empty-completions counter helper.
# --------------------------------------------------------------------------- #
def test_record_empty_completion_safe_when_disabled():
    # No init() needed — a no-op that never raises when metrics are off.
    metrics.record_empty_completion("smart", "length", recovered=False)
    metrics.record_empty_completion("", "", recovered=True)


def test_record_empty_completion_increments_when_enabled():
    if metrics.is_enabled() or _try_init():
        from prometheus_client import REGISTRY

        p = metrics._prefix()
        labels = {"model": "smart", "reason": "length", "recovered": "true"}
        before = REGISTRY.get_sample_value(f"{p}_llm_empty_completions_total", labels) or 0.0
        metrics.record_empty_completion("smart", "length", recovered=True)
        after = REGISTRY.get_sample_value(f"{p}_llm_empty_completions_total", labels) or 0.0
        assert after == before + 1.0


def _try_init() -> bool:
    try:
        metrics.init()
        return metrics.is_enabled()
    except Exception:
        return False  # already-registered or prometheus missing — skip


# --------------------------------------------------------------------------- #
# 3. The measurement script on inline fixtures with known answers.
# --------------------------------------------------------------------------- #
# Five calls, chosen so every metric has a hand-computable answer:
#   A over-8k tool-call turn; B over-8k run-ending cap-hit empty (recovered);
#   C small run-ending empty (not recovered); D normal tool-call; E over-8k run-ending text.
_LEDGER_ROWS = [
    {"output_tokens": 10000, "has_tool_calls": True, "text_chars": 50, "finish_reason": "tool_calls"},
    {"output_tokens": 12000, "has_tool_calls": False, "text_chars": 0, "finish_reason": "length", "recovered": True},
    {"output_tokens": 500, "has_tool_calls": False, "text_chars": 5, "finish_reason": "stop"},
    {"output_tokens": 2000, "has_tool_calls": True, "text_chars": 800, "finish_reason": "stop"},
    {"output_tokens": 9000, "has_tool_calls": False, "text_chars": 3000, "finish_reason": "stop"},
]

# The same five calls as a Langfuse generations export (tags + usage live elsewhere).
_LANGFUSE_ROWS = [
    {
        "usageDetails": {"output": 10000},
        "metadata": {
            "has_tool_calls": True,
            "text_chars": 50,
            "finish_reason": "tool_calls",
            "subagent": "review-finder",
        },
    },
    {
        "usageDetails": {"output": 12000},
        "metadata": {"has_tool_calls": False, "text_chars": 0, "finish_reason": "length", "recovered": True},
    },
    {"usageDetails": {"output": 500}, "metadata": {"text_chars": 5, "finish_reason": "stop"}},
    {
        "usageDetails": {"output": 2000},
        "metadata": {"has_tool_calls": True, "text_chars": 800, "finish_reason": "stop"},
    },
    {"usageDetails": {"output": 9000}, "metadata": {"text_chars": 3000, "finish_reason": "stop"}},
]

_PANELS = [
    {"panel": "p1", "step_s": {"find_correctness": 3600, "find_crossfile": 1800, "merge": 600}},
    {"panel": "p2", "step_s": {"find_removed_behavior": 7200}},
]


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(r) for r in rows)


def _assert_known_answers(report: dict):
    assert report["calls"] == 5
    assert report["output_tokens_total"] == 33500

    over = report["over_8k"]
    assert over["calls"] == 3  # 10000, 12000, 9000
    assert over["call_share"] == 0.6
    assert over["output_tokens"] == 31000
    assert round(over["token_share"], 4) == round(31000 / 33500, 4)
    assert over["by_turn_kind"] == {"tool_call": 1, "run_ending": 2}

    assert report["cap_hits"] == 1  # only the "length" call

    empty = report["empty"]
    assert (empty["calls"], empty["of"]) == (2, 5)  # the two run-ending blanks
    assert empty["by_turn_kind"] == {"tool_call": 0, "run_ending": 2}
    assert empty["recovered"] == 1  # only B carried recovered=True


def test_ledger_source_known_answers():
    calls, dropped = measure.load_calls(_jsonl(_LEDGER_ROWS), "ledger")
    assert dropped == 0
    report = measure.analyze(calls, panels=_jsonl(_PANELS))
    _assert_known_answers(report)
    finder = report["finder"]
    assert finder["panels"] == 2
    assert finder["finder_hours"] == 3.5  # (3600 + 1800 + 7200) / 3600, "merge" excluded
    assert finder["hours_per_panel"] == 1.75


def test_langfuse_source_matches_ledger():
    calls, _ = measure.load_calls(_jsonl(_LANGFUSE_ROWS), "langfuse")
    report = measure.analyze(calls)
    _assert_known_answers(report)


def test_cli_text_and_json_round_trip(tmp_path, capsys):
    rows_file = tmp_path / "rows.jsonl"
    rows_file.write_text(_jsonl(_LEDGER_ROWS))
    panels_file = tmp_path / "panels.jsonl"
    panels_file.write_text(_jsonl(_PANELS))

    assert measure.run([str(rows_file), "--source", "ledger", "--panels", str(panels_file), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    _assert_known_answers(report)

    assert measure.run([str(rows_file), "--source", "ledger"]) == 0
    text = capsys.readouterr().out
    assert "3 of 5" in text and "60.0%" in text  # over-8k share
    assert "Empty calls" in text and "2 of 5" in text


def test_window_filter_drops_rows_outside_since_until():
    rows = [
        {"output_tokens": 100, "has_tool_calls": False, "text_chars": 10, "at": "2026-10-20T00:00:00Z"},
        {"output_tokens": 200, "has_tool_calls": False, "text_chars": 10, "at": "2026-11-01T00:00:00Z"},
    ]
    since = measure._parse_dt("2026-10-30T00:00:00Z")
    calls, dropped = measure.load_calls(_jsonl(rows), "ledger", since=since)
    assert dropped == 1
    assert [c["output_tokens"] for c in calls] == [200]


@pytest.mark.parametrize("source, timestamp_key", [("ledger", "at"), ("langfuse", "startTime")])
@pytest.mark.parametrize(
    "since, until",
    [
        ("2026-10-30", "2026-11-08"),
        ("2026-10-30T00:00:00", "2026-11-08T00:00:00"),
        ("2026-10-30T02:00:00+02:00", "2026-11-07T19:00:00-05:00"),
    ],
)
def test_cli_window_handles_mixed_timezones(tmp_path, capsys, source, timestamp_key, since, until):
    timestamps = [
        "2026-10-29T23:59:59Z",  # before since
        "2026-10-30T00:00:00Z",  # inclusive since
        "2026-11-01T12:00:00",  # timezone-less row assumes UTC too
        "2026-11-08T02:00:00+02:00",  # inclusive until
        "2026-11-07T20:00:00-05:00",  # after until in UTC
        "2026-11-08T00:00:01Z",
    ]
    rows_file = tmp_path / "rows.jsonl"
    rows_file.write_text(_jsonl([{timestamp_key: stamp} for stamp in timestamps]), encoding="utf-8")

    assert measure.run([
        str(rows_file), "--source", source, "--since", since, "--until", until, "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["calls"] == 3
    assert report["dropped_outside_window"] == 3


def test_recovered_count_from_log_when_rows_lack_flag():
    # Empty rows with no recovered flag; recovery comes from the completion-guard log.
    rows = [
        {"output_tokens": 300, "has_tool_calls": False, "text_chars": 0, "finish_reason": "length"},
        {"output_tokens": 300, "has_tool_calls": False, "text_chars": 0, "finish_reason": "length"},
    ]
    log_text = (
        "[completion-guard] reasoning-only turn; retrying once with thinking off, 4000 chars dropped (nudge 1/3)\n"
        "[completion-guard] reasoning-only turn; retrying once with thinking off, 5000 chars dropped (nudge 1/3)\n"
        "[completion-guard] reasoning-only turn t2; letting the run end (review-finder)\n"
    )
    calls, _ = measure.load_calls(_jsonl(rows), "ledger")
    report = measure.analyze(calls, log_text=log_text)
    assert report["empty"]["calls"] == 2
    # Two retries, one give-up -> one recovered.
    assert report["empty"]["log_recovery"] == {"retries": 2, "gave_up": 1, "recovered": 1}
    assert report["empty"]["recovered"] == 1
