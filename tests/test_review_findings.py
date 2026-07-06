"""The findings convention (graph/review/findings.py, ADR 0077) — the contract the
code-review workflow's prompts emit and its consumers (craft skill, board review
gate) parse. The parser is the load-bearing piece: subagent replies are prose with
a JSON block somewhere inside, not clean JSON."""

from __future__ import annotations

import json

from graph.review.findings import (
    FINDINGS_CONTRACT,
    Finding,
    parse_findings,
    render_findings_markdown,
)

_ITEM = {
    "file": "graph/agent.py",
    "line": 42,
    "severity": "major",
    "category": "correctness",
    "claim": "Off-by-one in the retry budget.",
    "evidence": "for i in range(retries - 1):",
}


# ── parse: the JSON-in-prose extraction ───────────────────────────────────────


def test_parse_fenced_json_block():
    text = f"I reviewed the diff.\n\n```json\n{json.dumps([_ITEM])}\n```\n\nDone."
    found = parse_findings(text)
    assert len(found) == 1
    f = found[0]
    assert f.file == "graph/agent.py" and f.line == 42
    assert f.severity == "major" and f.category == "correctness"


def test_parse_bare_array_without_fence():
    text = "Findings: " + json.dumps([_ITEM]) + " — that's all."
    assert len(parse_findings(text)) == 1


def test_parse_prefers_the_fuller_array():
    # A reply that echoes a one-item list from an earlier step, then emits its
    # own two-item deliverable — the fuller list wins.
    small = json.dumps([_ITEM])
    big = json.dumps([_ITEM, {**_ITEM, "line": 99, "claim": "Second defect."}])
    text = f"Earlier step said:\n```json\n{small}\n```\nMy merged list:\n```json\n{big}\n```"
    assert len(parse_findings(text)) == 2


def test_parse_keeps_verdicts_when_a_plain_copy_is_reprinted_last():
    # The verify pass annotates the findings; the final report reprints the plain
    # (verdict-less) list AFTER the annotated one. Both are the same length, so a
    # bare last-wins tie-break would surface the plain copy and drop the computed
    # verdicts from the rendered report. The richer (verdict-bearing) list wins.
    annotated = json.dumps([{**_ITEM, "verdict": "confirmed", "note": "reproduced"}])
    plain = json.dumps([_ITEM])
    text = f"Verifier said:\n```json\n{annotated}\n```\nFinal list:\n```json\n{plain}\n```"
    found = parse_findings(text)
    assert len(found) == 1
    assert found[0].verdict == "confirmed"


def test_parse_empty_array_and_no_array_return_empty():
    assert parse_findings("Clean review.\n```json\n[]\n```") == []
    assert parse_findings("No JSON here at all.") == []
    assert parse_findings("") == []


def test_parse_tolerates_junk_items_and_coerces_fields():
    arr = [
        "not a dict",
        {"no": "claim key"},
        {"claim": "Bad line type.", "line": "not-a-number", "severity": "CATASTROPHIC"},
    ]
    found = parse_findings(json.dumps(arr))
    assert len(found) == 1
    assert found[0].line == 0
    assert found[0].severity == "minor"  # unknown severity → floor, not crash


def test_parse_normalizes_verifier_vocabulary():
    arr = [
        {**_ITEM, "verdict": "SUPPORTED"},
        {**_ITEM, "claim": "B", "verdict": "unsupported"},
        {**_ITEM, "claim": "C", "verdict": "plausible"},
        {**_ITEM, "claim": "D", "verdict": "nonsense"},
    ]
    verdicts = [f.verdict for f in parse_findings(json.dumps(arr))]
    assert verdicts == ["confirmed", "refuted", "uncertain", ""]


# ── contract + round-trip ─────────────────────────────────────────────────────


def test_contract_snippet_names_every_schema_field():
    for field_name in ("file", "line", "severity", "category", "claim", "evidence"):
        assert f'"{field_name}"' in FINDINGS_CONTRACT


def test_contract_documents_source_preservation():
    assert "`source`" in FINDINGS_CONTRACT and "protopatch" in FINDINGS_CONTRACT


def test_to_dict_round_trips_through_parse():
    f = Finding(file="a.py", line=3, severity="blocker", category="security", claim="X", evidence="Y")
    [back] = parse_findings(json.dumps([f.to_dict()]))
    assert back == f


def test_source_round_trips_and_is_omitted_when_empty():
    f = Finding(file="a.py", line=3, severity="major", category="bug", claim="X", evidence="Y", source="protopatch")
    d = f.to_dict()
    assert d["source"] == "protopatch"
    [back] = parse_findings(json.dumps([d]))
    assert back == f
    # An LLM panel finding has no source — the key stays out of the dict.
    assert "source" not in Finding(claim="X").to_dict()


def test_source_is_normalized_and_defaults_empty():
    [f] = parse_findings(json.dumps([{**_ITEM, "source": "  ProtoPatch "}]))
    assert f.source == "protopatch"
    [g] = parse_findings(json.dumps([_ITEM]))
    assert g.source == ""


# ── render: the human-facing report ───────────────────────────────────────────


def test_render_groups_by_severity_and_shows_verdicts():
    findings = [
        Finding(file="b.py", line=1, severity="minor", claim="Small thing."),
        Finding(file="a.py", line=9, severity="blocker", claim="Big thing.", verdict="confirmed", note="reproduced"),
    ]
    md = render_findings_markdown(findings)
    assert md.index("### Blocker") < md.index("### Minor")
    assert "`a.py:9`" in md and "**confirmed**" in md and "reproduced" in md


def test_render_empty_says_clean():
    assert "clean" in render_findings_markdown([]).lower()


def test_render_shows_source_alongside_category():
    md = render_findings_markdown(
        [Finding(file="a.py", line=1, severity="major", category="concurrency", claim="Race.", source="protopatch")]
    )
    assert "_[concurrency · protopatch]_" in md
