"""Guard the committed frozen PV4 fixtures (#4897).

These assertions run against the checked-in `evals/memory_fixtures/pv4/` artifacts,
not fakes: they are the acceptance criteria for the fixture-generation card — 40
cases, one loadable per-user store per referenced user, valid supersession links.
CI-safe: stores load with `embed_fn=None` (keyword-only), so no gateway is touched.

The fixture directory is resolved from this test file's own location rather than
from the ``evals`` package, so it reads the worktree/branch copy even when an
editable install resolves ``evals`` to a different checkout.
"""

import json
from pathlib import Path

from evals.memory_fixtures import FIELDS, load_fixture_store

PV4 = Path(__file__).resolve().parents[1] / "evals" / "memory_fixtures" / "pv4"
CASE_KEYS = {"id", "user_id", "question", "answer", "choices", "hard"}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_frozen_case_set_has_forty_cases():
    cases_path = PV4 / "cases.jsonl"
    assert cases_path.is_file(), f"missing frozen case set: {cases_path}"
    cases = _load_jsonl(cases_path)
    assert len(cases) == 40
    assert len({case["id"] for case in cases}) == 40
    for case in cases:
        assert CASE_KEYS <= set(case), f"{case.get('id')}: missing keys {CASE_KEYS - set(case)}"
        # score_pv4 requires the reference answer to be one of the non-empty choices.
        choices = [choice for choice in case["choices"] if choice and choice.strip()]
        assert case["answer"] in choices, f"{case['id']}: answer not among non-empty choices"
        assert isinstance(case["hard"], bool)


def test_every_referenced_user_store_loads():
    cases = _load_jsonl(PV4 / "cases.jsonl")
    user_ids = sorted({case["user_id"] for case in cases})
    assert user_ids, "no users referenced by the case set"
    for uid in user_ids:
        path = PV4 / f"user_{uid}.jsonl"
        assert path.is_file(), f"case references user {uid} but {path.name} is absent"
        # load_fixture_store validates duplicate IDs and dangling supersession links,
        # and restores every row verbatim — a clean load is the acceptance check.
        store = load_fixture_store(path, None)
        for row in _load_jsonl(path):
            restored = store.get(row["id"])
            assert restored is not None, f"user {uid}: row {row['id']} did not round-trip"
            assert {field: getattr(restored, field) for field in FIELDS} == row


def test_no_stray_user_files_beyond_the_case_set():
    """Every committed user store must be referenced by a case (no orphans)."""
    cases = _load_jsonl(PV4 / "cases.jsonl")
    referenced = {f"user_{case['user_id']}.jsonl" for case in cases}
    on_disk = {path.name for path in PV4.glob("user_*.jsonl")}
    assert on_disk == referenced, f"orphan/missing user files: {on_disk ^ referenced}"


def test_frozen_stores_load_with_fake_embeddings():
    """All twelve stores can rebuild vectors without a gateway or model call."""
    paths = sorted(PV4.glob("user_*.jsonl"))
    assert len(paths) == 12
    for path in paths:
        embedded = []

        def fake_embed(text):
            embedded.append(text)
            return [1.0, 0.0]

        store = load_fixture_store(path, fake_embed)
        rows = _load_jsonl(path)
        assert embedded == [row["content"] for row in rows]
        with store._connect() as db:
            vectors = db.execute("SELECT memory_id, vec FROM memory_vectors").fetchall()
        assert len(vectors) == len(rows)
        assert {row[0] for row in vectors} == {row["id"] for row in rows}
        assert all(json.loads(row[1]) == [1.0, 0.0] for row in vectors)
