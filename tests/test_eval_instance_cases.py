"""Tests for ADR 0126 E4a — per-instance eval cases.

The runner loads core ``evals/tasks.json`` plus the instance's own
``<config dir>/evals/cases.json`` when it exists; instance case ids are
namespaced ``instance:<id>`` so they can't collide with core ids; a malformed
file is a clear error naming it; ``--tasks`` selects an instance case by its
namespaced id; and ``evals.report`` shows instance cases under their own
heading, split by tier.

Run clean of ambient Langfuse creds:

    env -u LANGFUSE_PUBLIC_KEY -u LANGFUSE_SECRET_KEY -u LANGFUSE_HOST \\
        python -m pytest tests/test_eval_instance_cases.py -q
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evals import runner
from evals.compare import INSTANCE_ID_PREFIX, is_instance_case
from evals.report import build_report
from evals.sweep import _runner_cmd


def _write(path, cases):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases), encoding="utf-8")
    return path


# ── load: absent file → core only; present → merged + namespaced ────────────────


def test_absent_instance_file_is_core_only(tmp_path):
    """No cases.json → load_all_cases returns exactly the core tasks, unchanged."""
    core = _write(tmp_path / "tasks.json", [{"id": "core_a", "kind": "ask", "prompt": "hi"}])
    missing = tmp_path / "config" / "evals" / "cases.json"
    cases = runner.load_all_cases(core, instance_path=missing)
    assert [c["id"] for c in cases] == ["core_a"]
    assert all(not is_instance_case(c) for c in cases)


def test_instance_cases_are_merged_with_namespaced_ids(tmp_path):
    core = _write(tmp_path / "tasks.json", [{"id": "core_a", "kind": "ask", "prompt": "hi"}])
    inst = _write(
        tmp_path / "config" / "evals" / "cases.json",
        [
            {"id": "my_case", "kind": "ask", "prompt": "hi"},
            {"id": "other", "kind": "ask", "prompt": "hi"},
        ],
    )
    cases = runner.load_all_cases(core, instance_path=inst)
    ids = [c["id"] for c in cases]
    assert ids == ["core_a", "instance:my_case", "instance:other"]
    # Core case untouched; instance cases flagged and prefixed.
    by_id = {c["id"]: c for c in cases}
    assert not is_instance_case(by_id["core_a"])
    assert is_instance_case(by_id["instance:my_case"])
    assert by_id["instance:my_case"]["instance"] is True


def test_namespacing_avoids_a_collision_with_a_core_id(tmp_path):
    """An instance case sharing a core case's base id becomes a distinct id."""
    core = _write(tmp_path / "tasks.json", [{"id": "greeting", "kind": "ask", "prompt": "hi"}])
    inst = _write(tmp_path / "config" / "evals" / "cases.json", [{"id": "greeting", "kind": "ask", "prompt": "hi"}])
    cases = runner.load_all_cases(core, instance_path=inst)
    ids = [c["id"] for c in cases]
    assert ids == ["greeting", "instance:greeting"]
    assert len(set(ids)) == len(ids)  # no duplicate ids


def test_instance_case_tier_defaults_like_a_core_case(tmp_path):
    """tier defaulting from E1b carries over: a rubric case → tracked, else gate."""
    inst = _write(
        tmp_path / "cases.json",
        [
            {"id": "plain", "kind": "ask", "prompt": "hi"},
            {"id": "graded", "kind": "ask", "prompt": "hi", "verify_rubric": {"criteria": ["x"]}},
            {"id": "pinned", "kind": "ask", "prompt": "hi", "tier": "tracked"},
        ],
    )
    cases = {c["id"]: c for c in runner.load_instance_cases(inst)}
    from evals.compare import resolve_tier

    assert resolve_tier(cases["instance:plain"]) == "gate"
    assert resolve_tier(cases["instance:graded"]) == "tracked"
    assert resolve_tier(cases["instance:pinned"]) == "tracked"


# ── malformed file → clear error naming the file ────────────────────────────────


def test_malformed_json_errors_naming_the_file(tmp_path):
    bad = tmp_path / "config" / "evals" / "cases.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        runner.load_instance_cases(bad)
    assert str(bad) in str(exc.value)


def test_non_list_json_errors_naming_the_file(tmp_path):
    bad = _write(tmp_path / "cases.json", {"id": "x"})  # a dict, not a list
    with pytest.raises(ValueError) as exc:
        runner.load_instance_cases(bad)
    assert str(bad) in str(exc.value)


def test_case_without_an_id_errors_naming_the_file(tmp_path):
    bad = _write(tmp_path / "cases.json", [{"kind": "ask", "prompt": "hi"}])
    with pytest.raises(ValueError) as exc:
        runner.load_instance_cases(bad)
    assert str(bad) in str(exc.value)


def test_unknown_tier_in_instance_file_is_a_load_error_naming_the_case(tmp_path):
    bad = _write(tmp_path / "cases.json", [{"id": "oops", "tier": "nightly"}])
    with pytest.raises(ValueError) as exc:
        runner.load_instance_cases(bad)
    # resolve_tier names the (now-namespaced) case + the bad tier.
    assert "instance:oops" in str(exc.value)
    assert "nightly" in str(exc.value)


# ── config-dir resolution matches the server's ──────────────────────────────────


def test_instance_cases_path_is_under_the_resolved_config_dir():
    from infra.paths import instance_paths

    assert runner.instance_cases_path() == instance_paths().config_dir / "evals" / "cases.json"


def test_load_all_cases_reads_the_resolved_instance_config_dir(tmp_path):
    """Default path (no explicit instance_path) resolves the same config dir the
    server uses — proven by writing cases under instance_paths().config_dir."""
    from infra.paths import instance_paths

    core = _write(tmp_path / "tasks.json", [{"id": "core_a", "kind": "ask", "prompt": "hi"}])
    cases_file = instance_paths().config_dir / "evals" / "cases.json"
    _write(cases_file, [{"id": "live", "kind": "ask", "prompt": "hi"}])
    cases = runner.load_all_cases(core)
    assert [c["id"] for c in cases] == ["core_a", "instance:live"]


# ── --tasks selects an instance case ────────────────────────────────────────────


def test_tasks_filter_selects_an_instance_case(tmp_path):
    core = _write(tmp_path / "tasks.json", [{"id": "core_a", "kind": "ask", "prompt": "hi"}])
    inst = _write(
        tmp_path / "config" / "evals" / "cases.json",
        [{"id": "my_case", "kind": "ask", "prompt": "hi"}, {"id": "two", "kind": "ask", "prompt": "hi"}],
    )
    cases = runner.load_all_cases(core, instance_path=inst)
    selected, unknown = runner.select_by_ids(cases, "instance:my_case")
    assert [c["id"] for c in selected] == ["instance:my_case"]
    assert unknown == []


# ── run_one marks the row; report heading splits by tier ────────────────────────


def test_run_one_stamps_the_instance_flag_on_the_row():
    async def fake(client, case):
        return runner.CaseResult(case["id"], case["category"], case["name"], True, "ok")

    import evals.runner as r

    orig = r._RUNNERS["ask"]
    r._RUNNERS["ask"] = fake
    try:
        core_case = {"id": "core_a", "category": "tool", "name": "A", "kind": "ask", "prompt": "hi"}
        inst_case = {
            "id": f"{INSTANCE_ID_PREFIX}my_case",
            "category": "tool",
            "name": "I",
            "kind": "ask",
            "prompt": "hi",
            "instance": True,
        }
        assert asyncio.run(r.run_one(None, core_case)).instance is False
        assert asyncio.run(r.run_one(None, inst_case)).instance is True
    finally:
        r._RUNNERS["ask"] = orig


def test_save_report_marks_instance_rows(tmp_path):
    results = [
        runner.CaseResult("core_a", "tool", "A", passed=True, detail="", tier="gate"),
        runner.CaseResult("instance:my_case", "tool", "I", passed=True, detail="", tier="gate", instance=True),
    ]
    out = tmp_path / "run-x.json"
    runner._save_report(results, out, model="m", base_url="http://x")
    rep = json.loads(out.read_text())
    marks = {r["id"]: r["instance"] for r in rep["results"]}
    assert marks == {"core_a": False, "instance:my_case": True}


def _row(cid, passed, *, tier="gate", instance=False):
    return {"id": cid, "category": "tool", "name": cid, "passed": passed, "detail": "", "tier": tier, "instance": instance}


def _report(ts, model, rows):
    return {
        "ts": ts,
        "model": model,
        "total": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "results": rows,
    }


def test_report_shows_instance_cases_under_their_own_heading_split_by_tier():
    rows = [
        _row("core_a", True, tier="gate"),
        _row("instance:g1", True, tier="gate", instance=True),
        _row("instance:g2", False, tier="gate", instance=True),
        _row("instance:t1", True, tier="tracked", instance=True),
    ]
    md = build_report([_report("2026-10-29T00:00:00+00:00", "model/a", rows)])
    # Own heading, present and after the core trend.
    assert "## Instance cases" in md
    assert md.index("## Trend") < md.index("## Instance cases")
    # Split by tier: gate 1/2 and tracked 1/1 for the instance subset.
    section = md[md.index("## Instance cases") :]
    assert "Gate" in section and "Tracked" in section
    assert "1/2" in section and "1/1" in section


def test_report_has_no_instance_heading_without_instance_cases():
    rows = [_row("core_a", True), _row("core_b", False)]
    md = build_report([_report("2026-10-29T00:00:00+00:00", "model/a", rows)])
    assert "## Instance cases" not in md


def test_core_gate_headline_excludes_instance_cases():
    """The leaderboard/trend gate figure (and the model ranking) stays core-only;
    instance cases live only in their own section (ADR 0126 D5). A passing instance
    case must not inflate the core gate headline."""
    rows = [
        _row("core_a", False, tier="gate"),  # core gate: 0/1
        _row("instance:g1", True, tier="gate", instance=True),  # instance gate: 1/1
    ]
    md = build_report([_report("2026-10-29T00:00:00+00:00", "model/a", rows)])
    core_part = md[: md.index("## Instance cases")]
    assert "**0/1" in core_part  # core gate, bolded in the leaderboard, core-only
    assert "**1/2" not in core_part  # NOT inflated by the instance pass
    inst_part = md[md.index("## Instance cases") :]
    assert "1/1" in inst_part  # the instance pass shows only in its own section


# ── main's suite loader: --instance-cases merges; --tasks-file is self-contained ──


def test_load_suite_merges_instance_cases_from_an_explicit_path(tmp_path):
    """No --tasks-file: core ``tasks.json`` + the explicit ``--instance-cases`` file,
    instance ids namespaced. This is the path the sweep child takes (it is handed the
    operator instance's file because its own throwaway config dir has none)."""
    inst = _write(
        tmp_path / "config" / "evals" / "cases.json",
        [{"id": "my_case", "kind": "ask", "prompt": "hi"}],
    )
    tasks_path, cases = runner._load_suite(None, str(inst))
    ids = [c["id"] for c in cases]
    assert "instance:my_case" in ids
    assert any(not is_instance_case(c) for c in cases)  # core tasks.json still loaded
    assert tasks_path.name == "tasks.json"


def test_load_suite_tasks_file_is_self_contained(tmp_path):
    """An explicit --tasks-file loads ALONE — instance cases are not merged into
    someone else's suite, even when an instance-cases path is given."""
    self_contained = _write(tmp_path / "plugin_tasks.json", [{"id": "only", "kind": "ask", "prompt": "hi"}])
    inst = _write(tmp_path / "config" / "evals" / "cases.json", [{"id": "ignored", "kind": "ask", "prompt": "hi"}])
    tasks_path, cases = runner._load_suite(str(self_contained), str(inst))
    assert [c["id"] for c in cases] == ["only"]
    assert tasks_path == self_contained


# ── sweep → runner child: the operator cases file rides --instance-cases ──────────


def test_sweep_passes_the_operator_instance_cases_to_the_runner_child(tmp_path):
    """The sweep's runner subprocess boots on a THROWAWAY instance with no cases.json,
    so the operator instance's file must ride into the child on --instance-cases — or
    the child silently runs core only and reports a resolved ``instance:<id>`` unknown
    (ADR 0126 E4a blocking finding)."""
    inst = _write(
        tmp_path / "config" / "evals" / "cases.json",
        [{"id": "my_case", "kind": "ask", "prompt": "hi"}],
    )
    cmd = _runner_cmd(
        "http://127.0.0.1:7990",
        "vendor/m",
        tmp_path / "out.json",
        tasks="instance:my_case",
        instance_cases=inst,
    )
    assert "--instance-cases" in cmd
    # The flag's value is the operator instance's file, verbatim.
    assert cmd[cmd.index("--instance-cases") + 1] == str(inst)
    # And the --tasks selection the sweep resolved rides along, so the child runs it.
    assert cmd[cmd.index("--tasks") + 1] == "instance:my_case"


def test_sweep_omits_instance_cases_flag_when_the_file_is_absent(tmp_path):
    """No operator cases.json → the runner command is unchanged (a plain suite), so an
    instance with no cases behaves exactly as today."""
    missing = tmp_path / "config" / "evals" / "cases.json"
    cmd = _runner_cmd("http://127.0.0.1:7990", "vendor/m", tmp_path / "out.json", instance_cases=missing)
    assert "--instance-cases" not in cmd
