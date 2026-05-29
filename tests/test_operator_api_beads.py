from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from operator_api.beads import BeadsService


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_beads_status_detects_uninitialized_store(monkeypatch, tmp_path) -> None:
    def fake_run(*args, **kwargs):
        return _completed(
            args[0],
            returncode=1,
            stderr='{"error":{"code":"NOT_INITIALIZED"}}',
        )

    monkeypatch.setattr("operator_api.beads.subprocess.run", fake_run)

    assert BeadsService().status(str(tmp_path)) == {"initialized": False}


def test_beads_list_uses_br_json_and_filters_tombstones(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return _completed(
            args[0],
            stdout=(
                '[{"id":"bd-1","status":"open"},'
                '{"id":"bd-2","status":"tombstone"}]'
            ),
        )

    monkeypatch.setattr("operator_api.beads.subprocess.run", fake_run)

    issues = BeadsService().list(str(tmp_path))

    assert issues == [{"id": "bd-1", "status": "open"}]
    assert calls[0][0][0] == ["br", "list", "--all", "--json"]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["env"]["RUST_LOG"] == "error"


def test_beads_create_builds_structured_command(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return _completed(args[0], stdout='{"id":"bd-1","title":"Task"}')

    monkeypatch.setattr("operator_api.beads.subprocess.run", fake_run)

    issue = BeadsService().create(
        str(tmp_path),
        {
            "title": "Task",
            "type": "feature",
            "priority": 1,
            "description": "Details",
            "assignee": "agent",
        },
    )

    assert issue == {"id": "bd-1", "title": "Task"}
    assert calls[0] == [
        "br",
        "create",
        "Task",
        "--json",
        "--type",
        "feature",
        "--priority",
        "1",
        "--description",
        "Details",
        "--assignee",
        "agent",
    ]


def test_beads_update_close_delete_build_commands(monkeypatch, tmp_path) -> None:
    calls = []
    outputs = iter(
        [
            '{"id":"bd-1","status":"in_progress"}',
            '{"id":"bd-1","status":"closed"}',
            '{"deleted":"bd-1"}',
        ]
    )

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return _completed(args[0], stdout=next(outputs))

    monkeypatch.setattr("operator_api.beads.subprocess.run", fake_run)

    service = BeadsService()
    assert service.update(
        str(tmp_path),
        "bd-1",
        {
            "title": "Next",
            "status": "in_progress",
            "priority": 1,
            "type": "task",
            "assignee": "agent",
        },
    ) == {"id": "bd-1", "status": "in_progress"}
    assert service.close(str(tmp_path), "bd-1", "done") == {"id": "bd-1", "status": "closed"}
    assert service.delete(str(tmp_path), "bd-1") == {"deleted": "bd-1"}

    assert calls == [
        [
            "br",
            "update",
            "bd-1",
            "--json",
            "--title",
            "Next",
            "--status",
            "in_progress",
            "--priority",
            "1",
            "--type",
            "task",
            "--assignee",
            "agent",
        ],
        ["br", "close", "bd-1", "--json", "--reason", "done"],
        ["br", "delete", "bd-1", "--json"],
    ]


def test_beads_service_serializes_br_calls(monkeypatch, tmp_path) -> None:
    active = 0
    overlapped = False
    guard = Lock()

    def fake_run(*args, **kwargs):
        nonlocal active, overlapped
        with guard:
            active += 1
            overlapped = overlapped or active > 1
        time.sleep(0.01)
        with guard:
            active -= 1
        return _completed(args[0], stdout="[]")

    monkeypatch.setattr("operator_api.beads.subprocess.run", fake_run)

    service = BeadsService()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.status(str(tmp_path)), range(6)))

    assert results == [{"initialized": True}] * 6
    assert overlapped is False
