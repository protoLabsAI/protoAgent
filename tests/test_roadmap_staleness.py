"""Tests for scripts/check_roadmap_staleness.py (the #1945 roadmap-freshness CI guard).

The issue-state fetch is injected (``run(sections, fetch)``), so nothing here touches the
network — the guard's core contracts (closed-in-Planned flags, Shipped/release-ref/ref-less
never flag, API failure soft-fails) are exercised against fixture sections, plus one test
that the REAL roadmap.json parses into the expected ref-set shape.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_roadmap_staleness", Path(__file__).parent.parent / "scripts" / "check_roadmap_staleness.py"
)
staleness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(staleness)


# Fixture mirroring the real roadmap.json shape (see scripts/roadmap.py, which derives it
# from ROADMAP.md): sections of {status, items: [{title, detail, refs}]}.
_SECTIONS = [
    {
        "status": "Planned",
        "items": [
            {"title": "closed thing", "detail": "", "refs": ["#100"]},
            {"title": "open thing", "detail": "", "refs": ["#200"]},
            {"title": "no refs", "detail": "", "refs": []},
        ],
    },
    {
        "status": "In progress",
        "items": [
            {"title": "wip closed", "detail": "", "refs": ["#300", "v1.2.3"]},
        ],
    },
    {
        "status": "Shipped",
        "items": [
            {"title": "shipped issue ref", "detail": "", "refs": ["#400"]},
            {"title": "release ref", "detail": "", "refs": ["v0.98.0"]},
        ],
    },
]

_STATES = {100: "closed", 200: "open", 300: "closed", 400: "closed"}


def test_active_issue_refs_extracts_only_planned_and_in_progress() -> None:
    refs = staleness.active_issue_refs(_SECTIONS)
    assert refs == [
        ("Planned", "closed thing", 100),
        ("Planned", "open thing", 200),
        ("In progress", "wip closed", 300),
    ]


def test_release_refs_and_refless_items_never_yield_checks() -> None:
    # Acceptance criterion: vX.Y.Z refs and ref-less items can never false-positive —
    # they don't even reach the fetch.
    sections = [
        {
            "status": "Planned",
            "items": [
                {"title": "release only", "detail": "", "refs": ["v9.9.9"]},
                {"title": "bare", "detail": "", "refs": []},
            ],
        }
    ]
    assert staleness.active_issue_refs(sections) == []


def test_status_matching_is_case_and_hyphen_insensitive() -> None:
    sections = [{"status": "In-Progress", "items": [{"title": "x", "detail": "", "refs": ["#7"]}]}]
    assert staleness.active_issue_refs(sections) == [("In-Progress", "x", 7)]


def test_closed_refs_in_active_sections_are_flagged() -> None:
    stale, warnings = staleness.run(_SECTIONS, _STATES.__getitem__)
    assert warnings == []
    assert len(stale) == 2
    # The message names the stale item + ref and carries the rotate-into-Shipped guidance.
    assert "'closed thing'" in stale[0] and "#100" in stale[0]
    assert "rotate it into Shipped" in stale[0]
    assert "'wip closed'" in stale[1] and "#300" in stale[1]


def test_shipped_refs_are_not_checked_even_when_closed() -> None:
    fetched: list[int] = []

    def fetch(n: int) -> str:
        fetched.append(n)
        return _STATES[n]

    staleness.run(_SECTIONS, fetch)
    assert 400 not in fetched  # #400 is closed, but lives under Shipped


def test_all_open_passes() -> None:
    stale, warnings = staleness.run(_SECTIONS, lambda n: "open")
    assert stale == [] and warnings == []


def test_api_failure_soft_fails_as_warning_not_stale() -> None:
    # The guard must never brick unrelated CI on a network/rate-limit blip: an ApiError
    # becomes a warning (exit 0 in main), and the remaining refs are still checked.
    def fetch(n: int) -> str:
        if n == 100:
            raise staleness.ApiError("#100: GitHub API lookup failed (rate limited)")
        return _STATES[n]

    stale, warnings = staleness.run(_SECTIONS, fetch)
    assert len(warnings) == 1 and "#100" in warnings[0]
    assert len(stale) == 1 and "#300" in stale[0]  # #300 still flagged despite the #100 blip


def test_fetch_issue_state_rejects_unexpected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 whose body lacks a sane "state" must surface as ApiError (soft-fail), never
    # as a bogus open/closed verdict. Patched at the urllib seam — no network in tests.
    import io

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(json.dumps({"state": "weird"}).encode())  # BytesIO is its own context manager

    monkeypatch.setattr(staleness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(staleness.ApiError, match="unexpected issue state"):
        staleness.fetch_issue_state("protoLabsAI/protoAgent", 1)


def test_real_roadmap_json_parses_into_the_expected_ref_shape() -> None:
    # The committed roadmap must always be parseable by the guard (no live API call):
    # sections carry the known statuses, and every extracted ref is an int under an
    # active status with a non-empty title.
    sections = json.loads(staleness.ROADMAP_JSON.read_text(encoding="utf-8"))
    statuses = {s["status"] for s in sections}
    assert statuses == {"Planned", "In progress", "Shipped"}

    refs = staleness.active_issue_refs(sections)
    assert refs, "expected at least one Planned/In-progress issue ref in the live roadmap"
    for status, title, number in refs:
        assert staleness._is_active(status)
        assert title and isinstance(title, str)
        assert isinstance(number, int) and number > 0
    # No release ref may leak into the checkable set (the no-false-positive guarantee).
    all_active_refs = [
        r for s in sections if staleness._is_active(s["status"]) for i in s["items"] for r in i["refs"]
    ]
    assert len(refs) == len([r for r in all_active_refs if r.startswith("#")])
