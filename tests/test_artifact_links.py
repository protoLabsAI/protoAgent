"""Code links on mermaid artifacts (ADR 0038 amendment) — validation + per-version storage.

``show_artifact(..., links=…)`` validates every target the way ``show_code`` does (the fs fence,
the secret deny list, a real text file, an in-range line, a one-sentence note), DROPS a bad link
with a reason instead of failing the artifact, and stores the kept links WITH the version.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.test_artifact_plugin import _load
from tools.fs_tools import Project, ProjectRegistry

SEQ = "\n".join(
    [
        "sequenceDiagram",
        "  participant U as User",
        "  participant S as Server",
        "  U->>S: ask(question)",
        "  S->>S: run tools<br/>again",
        "  S-->>U: answer",
    ]
)


@pytest.fixture
def art(monkeypatch, tmp_path):
    """The plugin bound to a temp store, with a fenced project `demo` (and a sibling dir outside it)."""
    mod = _load(monkeypatch, tmp_path / "store")
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "agent.py").write_text("".join(f"line {i}\n" for i in range(1, 31)))
    (root / ".env").write_text("TOKEN=x\n")
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "outside.py").write_text("secret = 1\n")
    reg = ProjectRegistry([Project(name="demo", root=root.resolve())])
    monkeypatch.setattr(mod._links, "_registry", lambda: reg)
    mod._root = root
    return mod


def _link(**kw):
    return {"project": "demo", "path": "src/agent.py", "line": 3, **kw}


def _latest(art):
    return art._read_store()["artifacts"][0]["versions"][-1]


def test_valid_links_are_stored_with_the_version_and_echoed(art):
    out = art.show_artifact.invoke(
        {
            "kind": "mermaid",
            "code": SEQ,
            "links": {"msg:1": _link(end_line=5, note="the entry point"), "participant:Server": _link(line=10)},
        }
    )
    assert "Linked 2 diagram element(s)" in out
    assert "msg:1 → demo/src/agent.py:3-5 L3: `line 3`" in out  # the model can self-check the range
    v = _latest(art)
    assert v["links"] == {
        "msg:1": {"project": "demo", "path": "src/agent.py", "line": 3, "end_line": 5, "note": "the entry point"},
        "participant:Server": {"project": "demo", "path": "src/agent.py", "line": 10, "end_line": 10, "note": ""},
    }
    # end_line past EOF is clamped, like show_code.
    art.rewrite_artifact.invoke({"code": SEQ, "links": {"msg:2": _link(line=29, end_line=99)}})
    assert _latest(art)["links"]["msg:2"]["end_line"] == 30


@pytest.mark.parametrize(
    "spec, why",
    [
        (_link(path="../outside.py"), "escapes project"),
        (_link(path="/etc/passwd"), "must be relative"),
        (_link(project="nope"), "unknown project"),
        (_link(path=".env"), "looks like a secret"),
        (_link(path="src/missing.py"), "no such file"),
        (_link(path="src/blob.bin"), "binary file"),
        (_link(line=0), "out of range"),
        (_link(line=31), "out of range"),
        (_link(line="3"), "must be an integer"),
        (_link(line=True), "must be an integer"),
        (_link(end_line=2), "before line"),
        (_link(note="x" * 281), "one sentence"),
        ("src/agent.py:3", "must be an object"),
    ],
)
def test_a_bad_link_is_dropped_with_a_reason_and_the_artifact_still_lands(art, spec, why):
    out = art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": spec, "msg:3": _link()}})
    assert "Created mermaid artifact" in out
    assert "Dropped 1 link(s)" in out and why in out
    assert list(_latest(art)["links"]) == ["msg:3"]  # the good one survived


def test_a_symlink_out_of_the_fence_or_to_a_secret_is_refused(art, tmp_path):
    os.symlink(tmp_path / "outside.py", art._root / "src" / "sneaky.py")
    os.symlink(art._root / ".env", art._root / "src" / "notes.txt")
    out = art.show_artifact.invoke(
        {
            "kind": "mermaid",
            "code": SEQ,
            "links": {"msg:1": _link(path="src/sneaky.py", line=1), "msg:2": _link(path="src/notes.txt", line=1)},
        }
    )
    assert "Dropped 2 link(s)" in out
    assert "escapes project" in out and "looks like a secret" in out
    assert "links" not in _latest(art)


def test_links_as_a_json_string_and_garbage(art):
    out = art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": json.dumps({"msg:1": _link()})})
    assert "Linked 1" in out
    out = art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": "{not json"})
    assert "not valid JSON" in out and "Created mermaid artifact" in out
    assert "links" not in _latest(art)


def test_links_are_mermaid_only(art):
    out = art.show_artifact.invoke({"kind": "svg", "code": "<svg/>", "links": {"a": _link()}})
    assert "mermaid artifacts only" in out
    assert "links" not in _latest(art)


def test_keys_that_match_nothing_in_the_source_are_reported(art):
    flow = "flowchart TD\n  A[Start] --> B{Check}"
    out = art.show_artifact.invoke(
        {"kind": "mermaid", "code": flow, "links": {"B": _link(), "Z": _link(), "msg:1": _link()}}
    )
    assert "don't match anything" in out and "Z" in out and "msg:1" in out
    out = art.show_artifact.invoke(
        {
            "kind": "mermaid",
            "code": SEQ,
            "links": {
                "msg:3": _link(),
                "msg:4": _link(),  # only 3 messages
                "msg:answer": _link(),  # unique label → fine
                "msg:run tools again": _link(),  # a <br/> label, matched on its words
                "participant:Server": _link(),
                "participant:Nobody": _link(),
            },
        }
    )
    tail = out.split("don't match anything")[1]
    assert "msg:4" in tail and "participant:Nobody" in tail
    assert "msg:answer" not in tail and "msg:run tools again" not in tail and "msg:3" not in tail


def test_update_carries_links_rewrite_does_not_and_each_version_keeps_its_own(art):
    art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": _link(note="v1 link")}})
    out = art.update_artifact.invoke({"old_string": "answer", "new_string": "final answer"})
    assert "Carried over 1 code link" in out
    store = art._read_store()["artifacts"][0]
    assert store["versions"][1]["links"] == store["versions"][0]["links"]
    # A targeted edit that renames a linked element says so.
    art.show_artifact.invoke({"kind": "mermaid", "code": "flowchart TD\n  A --> B", "links": {"B": _link()}})
    out = art.update_artifact.invoke({"old_string": "--> B", "new_string": "--> C"})
    assert "no longer match anything" in out and "B" in out
    # links={} clears on an update; passing a map replaces.
    art.update_artifact.invoke({"old_string": "--> C", "new_string": "--> D", "links": {}})
    assert "links" not in _latest(art)
    # A rewrite is a new diagram: no carry-over, and the reply says so.
    art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": _link()}})
    out = art.rewrite_artifact.invoke({"code": SEQ + "\n  U->>S: again"})
    assert "were not carried over" in out
    vers = art._read_store()["artifacts"][0]["versions"]
    assert "links" in vers[0] and "links" not in vers[1]  # the older version keeps its links
    # get_artifact shows the current version's links (the take-over path).
    art.update_artifact.invoke(
        {"old_string": "U->>S: again", "new_string": "U->>S: again!", "links": {"msg:4": _link(line=7)}}
    )
    assert "Code links (1):\n  msg:4 → demo/src/agent.py:7" in art.get_artifact.invoke({})


def test_a_panel_edit_keeps_the_links(art):
    from fastapi.testclient import TestClient

    from tests.test_artifact_plugin import _app

    art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": _link()}})
    art_id = art._read_store()["artifacts"][0]["id"]
    r = TestClient(_app(art)).put(f"/api/plugins/artifact/artifact/{art_id}", json={"code": SEQ + "\n"})
    assert r.status_code == 200
    v = _latest(art)
    assert v["by"] == "user" and v["links"]["msg:1"]["line"] == 3


def test_no_filesystem_toolset_means_no_links(art, monkeypatch):
    monkeypatch.setattr(art._links, "_registry", lambda: None)
    out = art.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": _link()}})
    assert "no filesystem toolset" in out and "Created mermaid artifact" in out
    assert "links" not in _latest(art)


def test_the_resolver_is_the_live_fs_fence(monkeypatch, tmp_path):
    """Unpatched, links resolve through tools.fs_tools.live_project_registry — the same fence
    (and the same `filesystem.enabled` gate) every fs tool uses."""
    import tools.fs_tools as fs

    mod = _load(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(fs, "live_project_registry", lambda *a: calls.append(1) or ProjectRegistry([]))
    out = mod.show_artifact.invoke({"kind": "mermaid", "code": SEQ, "links": {"msg:1": _link()}})
    assert calls == [1]
    assert "unknown project 'demo'" in out


def test_link_key_hygiene(art):
    out = art.show_artifact.invoke(
        {"kind": "mermaid", "code": SEQ, "links": {"": _link(), "a\nb": _link(), "k" * 201: _link(), "msg:1": _link()}}
    )
    assert "Dropped 3 link(s)" in out
    assert list(_latest(art)["links"]) == ["msg:1"]
