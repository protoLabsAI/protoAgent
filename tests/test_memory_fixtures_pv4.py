"""Offline PV4 builder coverage: no network or paid model calls."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from evals.memory_fixtures import FIELDS, load_fixture_store
from evals.memory_fixtures.build_pv4 import DEFAULT_USERS, build, fetch_users, select_cases
from graph.memory_facts import extract_and_store_facts


def dataset():
    return [dict(user_id=uid, documents=[f"chat {i}" for i in range(200)],
                 question=f"question {q}", answer="yes", choices=["yes", "no", "", "maybe", "unknown"],
                 hard=q % 2 == 0)
            for uid in (126, 127, 128) for q in range(20)]


def fake_fetch(rows, offsets=None):
    def fetch(offset, length):
        if offsets is not None:
            offsets.append(offset)
        # Exercise short pages, which the builder must advance by actual size.
        return dict(rows=[dict(row=row) for row in rows[offset:offset + min(length, 7)]],
                    num_rows_total=len(rows))
    return fetch


def test_paging_and_selection():
    offsets = []
    rows = fetch_users((126, 128), fake_fetch(dataset(), offsets))
    assert offsets == list(range(0, 60, 7))
    assert set(rows) == {126, 128}
    cases = select_cases(rows, 20)
    assert all(case["hard"] for case in cases)
    assert Counter(case["user_id"] for case in cases) == {126: 10, 128: 10}
    assert len(DEFAULT_USERS) == len(set(DEFAULT_USERS)) == 12
    assert all(126 <= uid <= 155 for uid in DEFAULT_USERS)
    with pytest.raises(ValueError, match="missing users"):
        fetch_users((155,), fake_fetch(dataset()))
    with pytest.raises(ValueError, match="case count"):
        select_cases(rows, 100)


def test_build_deterministic_resume_force_and_reload(tmp_path, capsys):
    calls = []

    async def extract(chat, **kwargs):
        calls.append((chat, kwargs["source"], kwargs["as_of"]))

        async def facts(transcript, config):
            # Same subject, sufficiently different facts to avoid duplicate gating.
            return [{"subject": "favorite", "content": "I enjoy skiing" if transcript == "chat 0"
                     else "My passion is painting"}] if transcript in {"chat 0", "chat 1"} else []

        return await extract_and_store_facts(chat, extractor=facts, **kwargs)

    options = dict(users=(126, 127, 128), count=40, fetch=fake_fetch(dataset()), extract=extract, yes=True)
    asyncio.run(build(tmp_path / "one", **options))
    out = capsys.readouterr().out
    assert "Extraction calls: 600" in out
    # Each user keeps 2 rows (chat 0 superseded by chat 1); the count is reported.
    assert "user 126: 2 stored rows" in out
    assert "no durable facts" not in out
    assert len(calls) == 600
    for pos, uid in enumerate(options["users"]):
        group = calls[pos * 200:(pos + 1) * 200]
        assert [source for _, source, _ in group] == [f"pv4-u{uid}-c{i}" for i in range(200)]
        assert [chat for chat, _, _ in group] == [f"chat {i}" for i in range(200)]
        dates = [day for _, _, day in group]
        assert all(a < b for a, b in zip(dates, dates[1:]))
    first = (tmp_path / "one" / "cases.jsonl").read_bytes()
    cases = [json.loads(line) for line in first.splitlines()]
    assert len(cases) == 40
    assert len({case["id"] for case in cases}) == 40
    assert sorted(Counter(case["user_id"] for case in cases).values()) == [13, 13, 14]
    assert cases[0]["choices"][2] == ""
    asyncio.run(build(tmp_path / "two", **options))
    assert (tmp_path / "two" / "cases.jsonl").read_bytes() == first
    calls.clear()
    path = tmp_path / "one" / "user_126.jsonl"
    frozen = path.read_bytes()
    asyncio.run(build(tmp_path / "one", **options))
    assert not calls
    assert path.read_bytes() == frozen
    asyncio.run(build(tmp_path / "one", force=True, **options))
    assert len(calls) == 600

    exported = [json.loads(line) for line in frozen.splitlines()]
    assert len(exported) == 2
    assert all(set(row) == set(FIELDS) for row in exported)
    assert exported[0]["superseded_by"] == exported[1]["id"]
    embedded = []

    def embed(text):
        embedded.append(text)
        return [1.0, 0.0]

    store = load_fixture_store(path, embed)
    assert len(embedded) == 2
    assert store.path.parent != path.parent
    for row in exported:
        assert {field: getattr(store.get(row["id"]), field) for field in FIELDS} == row
    assert {row.id for row in store.search("skiing", include_superseded=True)} == {1, 2}
    assert [row.id for row in store.search("skiing", deliverable=True, include_superseded=True)] == [2]


def test_decline_and_interrupted_user_are_not_published(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("interrupted")

    options = dict(users=(126,), count=2, fetch=fake_fetch(dataset()), extract=fail)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    asyncio.run(build(tmp_path, **options))
    assert not list(tmp_path.iterdir())
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(build(tmp_path, yes=True, **options))
    assert not list(tmp_path.iterdir())


def _facts_extract(calls, fail_at=None):
    """Real consolidation over a fake extractor; optionally dies at one source."""

    async def extract(chat, **kwargs):
        if kwargs["source"] == fail_at:
            raise RuntimeError("session ended")
        calls.append(kwargs["source"])

        async def facts(transcript, config):
            index = int(transcript.split()[1])
            return [{"subject": f"topic {index % 7}", "content": f"fact number {index} about item {index * 13}"}]

        return await extract_and_store_facts(chat, extractor=facts, **kwargs)

    return extract


def test_interrupted_run_resumes_from_checkpoint_without_repeating_calls(tmp_path, capsys):
    """bd-9w23j: a board round deletes its worktree, so progress must live outside it and
    a rerun must pay only for the chats it has not extracted yet."""
    work = tmp_path / "cache"
    base = dict(users=(126, 127), count=2, fetch=fake_fetch(dataset()), yes=True)

    reference_calls = []
    asyncio.run(build(tmp_path / "reference", extract=_facts_extract(reference_calls), **base))
    assert len(reference_calls) == 400

    calls = []
    with pytest.raises(RuntimeError, match="session ended"):
        asyncio.run(build(tmp_path / "wt1", work_dir=work,
                          extract=_facts_extract(calls, fail_at="pv4-u127-c120"), **base))
    assert len(calls) == 320  # user 126 complete, user 127 through chat 119
    assert not (tmp_path / "wt1" / "user_127.jsonl").exists()
    capsys.readouterr()

    # A fresh worktree: nothing under the old output survives, the checkpoints do.
    calls.clear()
    asyncio.run(build(tmp_path / "wt2", work_dir=work, extract=_facts_extract(calls), **base))
    out = capsys.readouterr().out
    assert "Extraction calls: 80;" in out
    assert "resumed from checkpoint: 320 chats" in out
    assert calls == [f"pv4-u127-c{i}" for i in range(120, 200)]
    for name in ("user_126.jsonl", "user_127.jsonl", "cases.jsonl"):
        assert (tmp_path / "wt2" / name).read_bytes() == (tmp_path / "reference" / name).read_bytes()

    # A third fresh tree re-exports from the finished checkpoints with no model calls.
    calls.clear()
    asyncio.run(build(tmp_path / "wt3", work_dir=work, extract=_facts_extract(calls), **base))
    assert not calls
    assert (tmp_path / "wt3" / "user_127.jsonl").read_bytes() == (tmp_path / "reference" / "user_127.jsonl").read_bytes()

    # --force discards checkpoints and re-extracts everything.
    calls.clear()
    asyncio.run(build(tmp_path / "wt4", work_dir=work, force=True, extract=_facts_extract(calls), **base))
    assert len(calls) == 400


def test_failed_extraction_is_retried_and_never_checkpointed_as_done(tmp_path, capsys):
    """The runtime extractor swallows model errors; a chat whose call failed must not be
    frozen into the checkpoint as a chat with no facts."""
    from evals.memory_fixtures.build_pv4 import ExtractionFailed

    work = tmp_path / "cache"
    seen = []
    down = {"chat 5"}

    async def model(transcript, config):
        seen.append(transcript)
        if transcript in down:
            raise RuntimeError("429 rate limited")
        index = int(transcript.split()[1])
        return [{"subject": f"topic {index % 7}", "content": f"fact number {index} about item {index * 13}"}]

    options = dict(users=(126,), count=1, fetch=fake_fetch(dataset()), yes=True,
                   work_dir=work, extractor=model, retry_delay_s=0)
    with pytest.raises(ExtractionFailed, match="chat 5"):
        asyncio.run(build(tmp_path / "a", **options))
    assert seen.count("chat 5") == 3
    assert not (tmp_path / "a" / "user_126.jsonl").exists()

    # Back up: the rerun resumes AT the failed chat, not after it.
    down.clear()
    seen.clear()
    asyncio.run(build(tmp_path / "b", **options))
    assert seen == [f"chat {i}" for i in range(5, 200)]
    assert "pv4-u126-c5" in (tmp_path / "b" / "user_126.jsonl").read_text(encoding="utf-8")

    # A transient failure that clears within the retries finishes the user.
    flaky = {"chat 7"}

    async def flaky_model(transcript, config):
        if transcript in flaky:
            flaky.discard(transcript)
            raise RuntimeError("timeout")
        return await model(transcript, config)

    asyncio.run(build(tmp_path / "c", users=(126,), count=1, fetch=fake_fetch(dataset()), yes=True,
                      work_dir=tmp_path / "cache2", extractor=flaky_model, retry_delay_s=0))
    assert "chat 7: extraction failed (RuntimeError('timeout')); attempt 1/3" in capsys.readouterr().out
    assert (tmp_path / "c" / "user_126.jsonl").exists()


def test_checkpoint_from_other_extraction_code_is_not_resumed(tmp_path, monkeypatch):
    from evals.memory_fixtures import build_pv4

    work = tmp_path / "cache"
    calls = []
    options = dict(users=(126,), count=1, fetch=fake_fetch(dataset()), yes=True, work_dir=work)
    asyncio.run(build(tmp_path / "a", extract=_facts_extract(calls), **options))
    monkeypatch.setattr(build_pv4, "extraction_code_key", lambda: "changed extractor")
    calls.clear()
    asyncio.run(build(tmp_path / "b", extract=_facts_extract(calls), **options))
    assert len(calls) == 200


def test_checkpoint_for_different_chats_is_not_resumed(tmp_path):
    work = tmp_path / "cache"
    calls = []
    asyncio.run(build(tmp_path / "a", users=(126,), count=1, fetch=fake_fetch(dataset()), yes=True,
                      work_dir=work, extract=_facts_extract(calls)))
    changed = dataset()
    for row in changed:
        row["documents"] = [f"chat {i} v2" for i in range(200)]
    calls.clear()
    asyncio.run(build(tmp_path / "b", users=(126,), count=1, fetch=fake_fetch(changed), yes=True,
                      work_dir=work, extract=_facts_extract(calls)))
    assert len(calls) == 200


def test_default_work_dir_is_outside_the_checkout_and_keyed_by_model(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from evals.memory_fixtures import build_pv4

    monkeypatch.delenv(build_pv4.WORK_ROOT_ENV, raising=False)
    haiku = build_pv4.default_work_dir(SimpleNamespace(aux_model="anthropic-oauth:claude-haiku-5-5", model_name="m"))
    main = build_pv4.default_work_dir(SimpleNamespace(aux_model="", model_name="protolabs/smart"))
    repo = Path(build_pv4.__file__).resolve().parents[2]
    assert repo not in haiku.parents
    assert haiku.name == "anthropic-oauth_claude-haiku-5-5" and main.name == "protolabs_smart"
    monkeypatch.setenv(build_pv4.WORK_ROOT_ENV, str(tmp_path))
    assert build_pv4.default_work_dir(SimpleNamespace(aux_model="x", model_name="")) == tmp_path / "x"


def test_zero_fact_user_is_flagged(tmp_path, capsys):
    async def extract(chat, **kwargs):
        pass  # Extraction stores nothing — the gateway could also be broken.

    asyncio.run(build(tmp_path, users=(126,), count=1, fetch=fake_fetch(dataset()),
                      extract=extract, yes=True))
    out = capsys.readouterr().out
    assert "user 126: 0 stored rows (no durable facts" in out
    assert (tmp_path / "user_126.jsonl").read_text(encoding="utf-8") == ""


def test_bad_chat_count_fails_before_extraction(tmp_path):
    rows = dataset()[:1]
    rows[0]["documents"] = ["only one"]
    with pytest.raises(ValueError, match="expected 200"):
        asyncio.run(build(tmp_path, users=(126,), count=1, fetch=fake_fetch(rows), yes=True))


def test_cli_model_override(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from evals.memory_fixtures import build_pv4
    from graph.config import LangGraphConfig

    config = SimpleNamespace(aux_model="configured")
    monkeypatch.setattr(LangGraphConfig, "from_yaml", lambda path: config)
    seen = []

    async def capture(output, **kwargs):
        seen.append(kwargs["config"].aux_model)

    monkeypatch.setattr(build_pv4, "build", capture)
    build_pv4.main(["--yes", "--output", str(tmp_path)])
    build_pv4.main(["--yes", "--model", "chosen", "--output", str(tmp_path)])
    assert seen == ["configured", "chosen"]
    assert capsys.readouterr().out.splitlines() == [
        "Extraction model: configured", "Extraction model: chosen",
    ]


@pytest.mark.parametrize("status, attempts", [(429, 3), (502, 3), (503, 3), (504, 3), (404, 1)])
def test_fetch_page_bounds_http_retries(monkeypatch, status, attempts):
    from urllib.error import HTTPError
    from evals.memory_fixtures import build_pv4

    calls = []
    delays = []

    def fail(url, timeout):
        calls.append(url)
        raise HTTPError(url, status, "unavailable", {}, None)

    monkeypatch.setattr(build_pv4, "urlopen", fail)
    monkeypatch.setattr(build_pv4.time, "sleep", delays.append)
    with pytest.raises(HTTPError) as error:
        build_pv4.fetch_page(100, 100)
    assert error.value.code == status
    assert len(calls) == attempts
    assert len(set(calls)) == 1
    assert delays == [10, 20][:attempts - 1]


def test_fetch_page_recovers_after_transient_error(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from evals.memory_fixtures import build_pv4

    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 502, "unavailable", {}, None)
        return BytesIO(b'{"rows": [], "num_rows_total": 100}')

    monkeypatch.setattr(build_pv4, "urlopen", fetch)
    monkeypatch.setattr(build_pv4.time, "sleep", lambda delay: None)
    assert build_pv4.fetch_page(100, 100) == {"rows": [], "num_rows_total": 100}
    assert len(calls) == 2


def test_fetch_page_uses_test_split(monkeypatch):
    from io import BytesIO
    from urllib.parse import parse_qs, urlparse

    from evals.memory_fixtures import build_pv4

    def open_url(url, timeout):
        parsed = urlparse(url)
        assert parsed.netloc == "datasets-server.huggingface.co"
        assert parse_qs(parsed.query) == dict(dataset=["memorilla/PersonalizationV4"],
                                             config=["default"], split=["test"], offset=["100"], length=["100"])
        assert timeout == 60
        return BytesIO(b'{"rows": [], "num_rows_total": 100}')

    monkeypatch.setattr(build_pv4, "urlopen", open_url)
    assert build_pv4.fetch_page(100, 100) == {"rows": [], "num_rows_total": 100}


def test_loader_sparse_forward_links_and_invalid_links(tmp_path):
    path = tmp_path / "user.jsonl"
    rows = [dict(id=rid, kind="fact", subject="hobby", content="hobby skiing",
                 delivery="retrieved", review_state="pending", provenance="extracted",
                 source_session="pv4-u126-c0", valid_from="2000-01-01", superseded_by=link)
            for rid, link in [(12, 45), (45, None)]]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    store = load_fixture_store(path, None)
    assert store.get(12).superseded_by == 45
    assert [row.id for row in store.search("skiing", deliverable=True)] == [45]
    rows[0]["superseded_by"] = 99
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="dangling"):
        load_fixture_store(path, None)
