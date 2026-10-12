"""An inline artifact is never visible WITHOUT its placement, at any store write (#4076, ADR 0118 D2).

The Artifact panel follows only panel-placed artifacts (``headOf`` in ``shell.js``), and a record
with no ``placement`` key reads as a panel artifact (the pre-0118 shape). So if any write path
created an inline artifact first and stamped its placement in a SECOND write, a panel poll
landing between the two would follow the inline answer. These tests pin the invariant at every
write the F2 confirmation sequence makes: a same-chat inline create, an other-session (``/v1``,
text-only) inline create, ``show_service``, the render-status stamp, an edit, a rewrite and a pin.

Each write is checked through the lock-free reader's own view (``_read_store`` straight after the
atomic replace), which is exactly what the panel's ``/history`` poll serves."""

from __future__ import annotations

from tests.test_artifact_inline_verify import CONSOLE, TEXT_ONLY
from tests.test_artifact_plugin import _client, _load


def _watch_writes(monkeypatch, art) -> list[dict]:
    """Record the store a lock-free reader sees right after every ``_write_store``."""
    seen: list[dict] = []
    orig = art._store._write_store

    def spy(store):
        orig(store)
        seen.append(art._store._read_store())

    monkeypatch.setattr(art._store, "_write_store", spy)
    return seen


def _placements(snapshot: dict) -> dict[str, str | None]:
    return {a["id"]: a.get("placement") for a in snapshot["artifacts"]}


def test_no_write_ever_exposes_an_inline_artifact_without_its_placement(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0
    seen = _watch_writes(monkeypatch, art)
    c = _client(art)

    art.show_artifact.invoke({"kind": "markdown", "code": "# Panel Doc", "placement": "panel", "state": CONSOLE})
    doc = art._read_store()["current"]
    c.post("/api/plugins/artifact/render-status", json={"id": doc, "version": 1, "ok": True})

    art.show_artifact.invoke({"kind": "svg", "code": "<svg/>", "placement": "inline", "state": CONSOLE})
    tile = art._read_store()["current"]
    c.post("/api/plugins/artifact/render-status", json={"id": tile, "version": 1, "ok": True})

    # Another client (/v1 — renders text only) makes an inline html answer.
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Intruder</h1>", "placement": "inline", "state": TEXT_ONLY})
    intruder = art._read_store()["current"]
    # A service caller (default placement → inline for a frame kind).
    svc = art.show_service(kind="vega-lite", code='{"mark":"bar","data":{"values":[]}}')["id"]

    art.update_artifact.invoke(
        {"old_string": "Intruder", "new_string": "Box", "artifact_id": intruder, "state": TEXT_ONLY}
    )
    art.rewrite_artifact.invoke({"code": "<svg><g/></svg>", "artifact_id": tile, "state": CONSOLE})
    c.post("/api/plugins/artifact/render-status", json={"id": tile, "version": 2, "ok": True})
    art.pin_artifact.invoke({"artifact_id": intruder})

    inline = {tile, intruder, svc}
    assert len(seen) >= 10, "every create/stamp/edit/pin above is a store write"
    for i, snap in enumerate(seen):
        places = _placements(snap)
        for aid in inline & places.keys():
            assert places[aid] == "inline", f"write #{i} exposed inline artifact {aid} with placement {places[aid]!r}"
        if doc in places:
            assert places[doc] is None, "a panel artifact keeps the pre-0118 shape (no placement key)"


def test_the_create_is_one_write_carrying_placement_and_current_together(monkeypatch, tmp_path):
    """The create itself: ONE write makes the record, its placement and ``current`` visible at
    once — there is no intermediate store where the new artifact is current but unplaced."""
    art = _load(monkeypatch, tmp_path)
    art._render_status._LAST_POLL_TS = 0
    art.show_artifact.invoke({"kind": "markdown", "code": "# Doc", "placement": "panel", "state": CONSOLE})
    seen = _watch_writes(monkeypatch, art)
    art.show_artifact.invoke({"kind": "html", "code": "<p>x</p>", "placement": "inline", "state": TEXT_ONLY})
    assert len(seen) == 1
    snap = seen[0]
    assert snap["current"] == snap["artifacts"][0]["id"]
    assert snap["artifacts"][0]["placement"] == "inline"
