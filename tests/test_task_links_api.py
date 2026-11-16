"""``parse_links`` reads an issue's links whether they arrive as the stored JSON string or
as an already-decoded list, so no reader can mistake one shape for "no links"."""

import json

import pytest

from tasks.autoclose import sweep_task_links
from tasks.stall import is_driven, task_activity
from tasks.store import TaskStore, parse_links


def test_parse_links_reads_the_stored_json_string():
    assert parse_links({"links": '[{"ref": "bd-5a"}, {"kind": "no-ref"}]'}) == [{"ref": "bd-5a"}]


def test_parse_links_accepts_an_already_decoded_list():
    assert parse_links({"links": [{"ref": "bd-5a"}, {"x": 1}, "junk"]}) == [{"ref": "bd-5a"}]


def test_parse_links_tolerates_empty_and_malformed_values():
    assert parse_links({"links": ""}) == []
    assert parse_links({"links": "{bad"}) == []
    assert parse_links({"links": '{"ref": "x"}'}) == []
    assert parse_links({}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_plain_urls_are_informational_but_github_prs_drive_and_sweep(tmp_path, legacy):
    store = TaskStore(db_path=str(tmp_path / "issues.db"))
    refs = [
        "https://example.com/x",
        "http://example.com/x",
        "https://github.com/acme/widgets/issues/1",
        "https://github.com.evil.test/acme/widgets/pull/1",
        "https://github.com/acme/widgets/pull/nope",
        "https://github.com/acme/widgets/pull/1",
    ]
    for ref in refs:
        row = store.create("linked", links=[ref])
        if legacy:
            # Simulate bytes persisted by the old writer, bypassing normalization.
            with store._conn:
                store._conn.execute(
                    "UPDATE issues SET links=? WHERE id=?",
                    (json.dumps([{"ref": ref, "kind": "pr", "state": "open", "reason": ""}]), row["id"]),
                )
        row = store.update(row["id"], status="in_progress")
        is_pr = ref == refs[-1]
        assert parse_links(row)[0]["kind"] == ("pr" if is_pr else "url")
        assert is_driven(row, sources={}) is is_pr
        assert task_activity(row, now=row["updated_at"], sources={}) == ("driven" if is_pr else "idle")

    polled = []

    async def pr_state(url):
        polled.append(url)
        return "open"

    assert await sweep_task_links(store, pr_state=pr_state) == 0
    assert polled == [refs[-1]]


@pytest.mark.parametrize("replace_links", [False, True])
def test_informational_urls_do_not_block_card_completion(tmp_path, replace_links):
    store = TaskStore(db_path=str(tmp_path / "issues.db"))
    url = "https://example.com/x"
    refs = ["bd-1", url] + (["job-2"] if replace_links else [])
    row = store.create("ship", links=refs)
    store.resolve_link("bd-1", "merged")
    if replace_links:
        store.update(row["id"], links=["bd-1", url])
    closed = store.get(row["id"])
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "bd-1 merged"
    assert parse_links(closed)[1]["state"] == "open"

    informational = store.create("notes", links=[url])
    assert store.update(informational["id"], links=[url])["status"] == "open"
    assert store.resolve_link(url, "read")[0]["status"] == "open"
