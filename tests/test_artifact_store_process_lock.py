"""The artifact store is serialised across PROCESSES, not just threads.

``_store.serialized`` used to hold a thread lock only, but the store file is shared by two
processes under the ACP runtime: the tools run in the operator-MCP process while the panel's
routes are served by the main one. Found in review of #3456: a ``/render-status`` stamp in one
process read the store, a ``pin_artifact`` in another pinned and replied "Pinned", and the stamp's
write-back erased the pin. Any write could be lost the same way, versions included.

These tests drive the store from REAL separate processes (multiprocessing "spawn" — the start
method Windows and macOS default to anyway) and force the interleaving that loses a write, rather
than hoping for it: a process that has READ the store waits, inside its read-modify-write window,
until the other process has read too. Without a cross-process lock that second read happens at
once, both processes hold the same snapshot, and whichever writes second erases the other. With
the lock the second process can't reach its read; the first one's wait times out (``_HOLD_S`` —
the only timed wait here, and it only costs time on the PASSING path) and the writes land one
after the other.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "artifact"
_PKG = "artifact_proc_under_test"
_HOLD_S = 1.0  # how long a process sits in its read-modify-write window waiting for the other to read
_START_S = 90  # a spawned child's cold start + imports (generous: a loaded Windows runner is slow)
_CTX = multiprocessing.get_context("spawn")


def _import_plugin():
    """The plugin as a fresh package (the host loader's way), bound to whatever ARTIFACT_DIR the
    environment names — used identically by the test process and every child process."""
    for k in [k for k in sys.modules if k.startswith(_PKG)]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        _PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    mod._render_status._RENDER_WAIT_MS = 0  # no browser: never wait on a render verdict
    return mod


# ── the child process ────────────────────────────────────────────────────────────────


def _install_gate(art, gate) -> None:
    """Wrap the store READ to force an interleaving (every reader in the plugin goes through
    the module-qualified ``_store._read_store``, so this catches them all).

    ``("hold", i_read, other_read)``: after my first read, say so and wait (up to _HOLD_S) for
    the other process to have read. ``("signal", i_read)``: after my first read, say so.
    ``("barrier", barrier)``: every read meets the other processes' reads at a barrier; once it
    times out it stays broken, so a serialised run pays the timeout once and then flows."""
    kind = gate[0]
    real = art._store._read_store
    first = [True]

    def gated():
        out = real()
        if kind == "barrier":
            try:
                gate[1].wait(_HOLD_S)
            except threading.BrokenBarrierError:
                pass  # serialised: the others can't be reading, which is the point
        elif first[0]:
            first[0] = False
            gate[1].set()
            if kind == "hold":
                gate[2].wait(_HOLD_S)
        return out

    art._store._read_store = gated


def _op_show(art, code):
    return art.show_artifact.invoke({"kind": "html", "code": code})


def _op_pin(art, artifact_id):
    return art.pin_artifact.invoke({"artifact_id": artifact_id})


def _op_render_status(art, artifact_id, version, error):
    """The panel's POST /render-status handler — the route itself, called the way FastAPI calls
    a sync ``def`` route (in a worker thread of the serving process)."""
    router = art._build_data_router()
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", None) == "/render-status")
    return endpoint(body={"id": artifact_id, "version": version, "ok": False, "error": error})


def _op_worker(art, index, rounds, shared):
    """Create one artifact of my own, then bump my slot in the SHARED artifact rounds-1 times.
    One store read per call, so every worker reads exactly ``rounds`` times."""
    replies = [art.show_artifact.invoke({"kind": "html", "code": f"<p>{index}</p>", "title": f"own-{index}"})]
    for r in range(1, rounds):
        replies.append(
            art.update_artifact.invoke(
                {"old_string": f"slot{index}={r - 1};", "new_string": f"slot{index}={r};", "artifact_id": shared}
            )
        )
    return replies


def _op_rewrite_many(art, artifact_id, n, size):
    for i in range(n):
        art.rewrite_artifact.invoke({"code": f"<!-- v{i + 2} -->" + "x" * size, "artifact_id": artifact_id})
    return n


def _op_hold_lock(art, holding, release, max_hold_s):
    """A holder wedged mid-save: take the store lock, read the store, say so, then sit on the lock
    until released (or ``max_hold_s``) — and only then write back its by-now-stale snapshot plus
    its own artifact, the way a real read-modify-write would."""
    with art._store._store_lock():
        store = art._store._read_store()
        holding.set()
        release.wait(max_hold_s)
        v = {"code": "<p>held</p>", "ts": 1, "by": "agent"}
        store["artifacts"].insert(
            0, {"id": "a-held", "title": "held", "kind": "html", "versions": [v], "version_count": 1}
        )
        art._store._write_store(store)
    return os.getpid()


def _op_show_timed(art, code):
    t0 = time.monotonic()
    reply = art.show_artifact.invoke({"kind": "html", "code": code})
    return reply, time.monotonic() - t0


def _op_read_raw(art, stop, reading):
    """Read history.json the way every read-only path does — the plugin's own lock-free read,
    including its Windows handling — as fast as possible, and parse it STRICTLY
    (``_read_store`` would swallow a torn file as an empty store)."""
    reads, torn = 0, []
    while not stop.is_set():
        raw = art._store._read_store_text()
        try:
            json.loads(raw)
        except ValueError as e:
            torn.append(f"{len(raw)} bytes: {e}")
        reads += 1
        reading.set()
    return reads, torn


_OPS = {
    "show": _op_show,
    "pin": _op_pin,
    "render_status": _op_render_status,
    "worker": _op_worker,
    "rewrite_many": _op_rewrite_many,
    "read_raw": _op_read_raw,
    "hold_lock": _op_hold_lock,
    "show_timed": _op_show_timed,
}


def _child(env, op, args, gate, patch, ready, go, out):
    """One store client in its own process: load the plugin, arm the gate, report ready, and
    run ``op`` once released — so process start-up never eats into a timed window. ``patch``
    sets ``_store`` module knobs (e.g. the lock bound) in this process."""
    try:
        os.environ.update(env)
        os.environ.pop("PROTOAGENT_INSTANCE", None)
        art = _import_plugin()
        for name, value in (patch or {}).items():
            setattr(art._store, name, value)
        if gate is not None:
            _install_gate(art, gate)
        ready.set()
        if not go.wait(_START_S):
            out.put(("error", "never released"))
            return
        out.put(("ok", _OPS[op](art, **args)))
    except BaseException:  # noqa: BLE001 — report everything back to the test process
        out.put(("error", traceback.format_exc()))


class _Client:
    def __init__(self, env, op, args, gate, patch=None):
        self.ready, self.go, self.out = _CTX.Event(), _CTX.Event(), _CTX.Queue()
        self.proc = _CTX.Process(
            target=_child, args=(env, op, args, gate, patch, self.ready, self.go, self.out), daemon=True
        )
        self.proc.start()

    def result(self):
        status, value = self.out.get(timeout=_START_S)
        self.proc.join(_START_S)
        assert status == "ok", value
        return value


@pytest.fixture
def art(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifact-store"))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    return _import_plugin()


@pytest.fixture
def procs(tmp_path):
    """Start store clients in child processes sharing this test's store (and its env knobs)."""
    started: list[_Client] = []

    def start(op, args, gate=None, patch=None, **env):
        c = _Client({"ARTIFACT_DIR": str(tmp_path / "artifact-store"), **env}, op, args, gate, patch)
        started.append(c)
        return c

    def wait_ready():
        for c in started:
            assert c.ready.wait(_START_S), "a child process never came up"

    start.wait_ready = wait_ready
    yield start
    for c in started:
        if c.proc.is_alive():
            c.proc.terminate()
            c.proc.join(10)


def _show(art, code, title=""):
    art.show_artifact.invoke({"kind": "html", "code": code, "title": title})
    return art._read_store()["current"]


# ── the lost writes ──────────────────────────────────────────────────────────────────


def test_render_status_in_one_process_does_not_erase_a_pin_from_another(art, procs):
    """The #3456 review's reproduction: the panel's /render-status (main process) holds its
    read-modify-write window open while pin_artifact runs in another process. Both must land —
    not a "Pinned" reply over a store that ended up unpinned."""
    aid = _show(art, "<h1>Resume</h1>", "Master resume")
    stamp_read, pin_read = _CTX.Event(), _CTX.Event()
    stamp = procs("render_status", {"artifact_id": aid, "version": 1, "error": "Icon is not defined"},
                  gate=("hold", stamp_read, pin_read))
    pin = procs("pin", {"artifact_id": aid}, gate=("signal", pin_read))
    procs.wait_ready()
    stamp.go.set()
    assert stamp_read.wait(_START_S)  # the stamp has read the store and is inside its window
    pin.go.set()

    assert stamp.result() == {"ok": True, "recorded": True}
    assert "Pinned artifact" in pin.result()
    a = art._find(art._read_store(), aid)
    assert a.get("pinned") is True, "the pin replied 'Pinned' but the store lost it"
    assert a["versions"][0].get("render", {}).get("error") == "Icon is not defined", "the render stamp was lost"


def test_n_processes_creating_and_editing_concurrently_lose_nothing(art, procs):
    """N processes each create an artifact and append versions to ONE shared artifact, every
    store read meeting the others' at a barrier. Every create and every version must survive,
    and no two edits may report the same version number."""
    n, rounds = 4, 4
    shared = _show(art, "".join(f"slot{i}=0;" for i in range(n)), "shared")
    barrier = _CTX.Barrier(n)
    workers = [
        procs("worker", {"index": i, "rounds": rounds, "shared": shared}, gate=("barrier", barrier)) for i in range(n)
    ]
    procs.wait_ready()
    for w in workers:
        w.go.set()
    replies = [w.result() for w in workers]

    flat = [r for rs in replies for r in rs]
    assert all(r.startswith(("Created html artifact", "Updated artifact")) for r in flat), flat
    store = art._read_store()
    assert {a["title"] for a in store["artifacts"]} == {"shared", *(f"own-{i}" for i in range(n))}
    s = art._find(store, shared)
    assert s["versions"][-1]["code"] == "".join(f"slot{i}={rounds - 1};" for i in range(n))
    assert len(s["versions"]) == s["version_count"] == 1 + n * (rounds - 1)
    reported = sorted(int(r.split("version ")[1].rstrip(".")) for r in flat if r.startswith("Updated"))
    assert reported == list(range(2, 2 + n * (rounds - 1)))


@pytest.mark.parametrize("first", ["pin", "evict"])
def test_eviction_racing_a_pin_never_strands_or_deletes_a_referenced_blob(art, procs, monkeypatch, tmp_path, first):
    """A file artifact sits at the eviction edge while one process pins it and another's new
    artifact evicts it (history=2). Whichever takes the store first, the outcome must be one of
    the two serial ones — never a pin that replied "Pinned" over an evicted artifact, never a
    surviving version whose blob the other process's sweep deleted, and never a lost create."""
    monkeypatch.setenv("ARTIFACT_HISTORY", "2")
    f = tmp_path / "resume.txt"
    f.write_bytes(b"resume bytes")
    fid = art.save_file_artifact.invoke({"path": str(f)}).split("Saved file artifact ")[1].split(" ")[0]
    _show(art, "<p>newer</p>")  # [newer, file] — the file artifact is next out

    first_read, second_read = _CTX.Event(), _CTX.Event()
    hold, signal = ("hold", first_read, second_read), ("signal", second_read)
    pin = procs("pin", {"artifact_id": fid}, gate=hold if first == "pin" else signal, ARTIFACT_HISTORY="2")
    evict = procs("show", {"code": "<p>newest</p>"}, gate=signal if first == "pin" else hold, ARTIFACT_HISTORY="2")
    procs.wait_ready()
    (pin if first == "pin" else evict).go.set()
    assert first_read.wait(_START_S)
    (evict if first == "pin" else pin).go.set()
    pin_reply, show_reply = pin.result(), evict.result()

    store = art._read_store()
    ids = {a["id"] for a in store["artifacts"]}
    assert show_reply.split("Created html artifact ")[1].split(" ")[0] in ids, "the evicting create was lost"
    for a in store["artifacts"]:  # nothing the store references was swept
        for v in a["versions"]:
            if v.get("blob"):
                assert art._blob_path(a["id"], v["blob"]).is_file(), f"{a['id']} references a deleted blob"
    if first == "pin":
        assert "Pinned artifact" in pin_reply
        kept = art._find(store, fid)
        assert kept is not None and kept.get("pinned") is True, "the pin replied 'Pinned' but was evicted"
        assert art._blob_path(fid, kept["versions"][-1]["blob"]).read_bytes() == b"resume bytes"
    else:  # the eviction took the store first, so the artifact was already gone when the pin looked
        assert "No artifact" in pin_reply, pin_reply
        assert fid not in ids and not (art._blob_root() / fid).exists()


def test_a_lock_free_reader_never_sees_a_torn_store(art, procs):
    """The panel polls the store without the lock, so every write must swap in a whole file:
    a reader in another process, reading as fast as it can across 30 rewrites of a store that
    grows past a megabyte, must parse every read."""
    aid = _show(art, "<!-- v1 -->")
    stop, reading = _CTX.Event(), _CTX.Event()
    reader = procs("read_raw", {"stop": stop, "reading": reading})
    writer = procs("rewrite_many", {"artifact_id": aid, "n": 30, "size": 64_000})
    procs.wait_ready()
    reader.go.set()
    assert reading.wait(_START_S)
    writer.go.set()
    try:
        assert writer.result() == 30
    finally:
        stop.set()
    reads, torn = reader.result()
    assert torn == [], f"{len(torn)}/{reads} reads saw a torn store: {torn[:3]}"
    a = art._find(art._read_store(), aid)
    assert a["versions"][-1]["code"].startswith("<!-- v31 -->") and a["version_count"] == 31


# ── lock scope + mechanics ───────────────────────────────────────────────────────────


def test_the_inline_render_verdict_is_not_starved_by_the_store_lock(art, monkeypatch):
    """show/update/rewrite wait briefly for the panel's render verdict when a panel is live —
    and /render-status, which writes that verdict, takes the store lock. The wait must happen
    AFTER the tool releases it: waiting while holding it blocked the very write it was waiting
    for, so the verdict never arrived inline (and every other writer stalled for the wait)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(art._build_data_router(), prefix="/api/plugins/artifact")
    client = TestClient(app)
    monkeypatch.setattr(art._render_status, "_RENDER_WAIT_MS", 5000)
    monkeypatch.setattr(art._render_status, "_LAST_POLL_TS", art._now())  # a panel is polling
    waiting = threading.Event()
    real_await = art._render_status._await_render

    def spy(*args, **kwargs):
        waiting.set()
        return real_await(*args, **kwargs)

    monkeypatch.setattr(art._render_status, "_await_render", spy)
    out: dict[str, str] = {}
    tool = threading.Thread(target=lambda: out.update(reply=art.show_artifact.invoke({"kind": "react", "code": "x"})))
    tool.start()
    assert waiting.wait(10)
    aid = art._read_store()["current"]
    r = client.post(
        "/api/plugins/artifact/render-status",
        json={"id": aid, "version": 1, "ok": False, "error": "Icon is not defined"},
    )
    tool.join(10)
    assert r.json()["recorded"] is True
    assert "FAILED to render" in out["reply"] and "Icon is not defined" in out["reply"], out


def _data_client(art):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(art._build_data_router(), prefix="/api/plugins/artifact")
    return TestClient(app)


def _at_the_cap(art, monkeypatch, cap=3):
    """An artifact holding exactly ``cap`` versions (v1..v{cap}), so every further commit trims
    the front and shifts each surviving version down one position."""
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", str(cap))
    aid = _show(art, "<p>v1</p>")
    for i in range(2, cap + 1):
        art.update_artifact.invoke({"old_string": f"v{i - 1}<", "new_string": f"v{i}<", "artifact_id": aid})
    assert len(art._find(art._read_store(), aid)["versions"]) == cap
    return aid


def test_a_verdict_is_never_borrowed_from_the_edit_that_shifted_this_one(art, monkeypatch):
    """At the version cap, edit A commits and waits (outside the lock) for its verdict; a panel
    edit B lands meanwhile, and the trim moves B into the slot A reported. B's verdict must not
    come back as A's — A's content is fine. A was superseded, so its reply carries no verdict."""
    aid = _at_the_cap(art, monkeypatch)
    client = _data_client(art)
    monkeypatch.setattr(art._render_status, "_RENDER_WAIT_MS", 5000)
    monkeypatch.setattr(art._render_status, "_LAST_POLL_TS", art._now())  # a panel is polling
    waiting = threading.Event()
    real_await = art._render_status._await_render

    def spy(*args, **kwargs):
        waiting.set()
        return real_await(*args, **kwargs)

    monkeypatch.setattr(art._render_status, "_await_render", spy)
    out: dict[str, str] = {}
    edit_a = threading.Thread(
        target=lambda: out.update(
            reply=art.update_artifact.invoke({"old_string": "v3<", "new_string": "A: fine<", "artifact_id": aid})
        )
    )
    edit_a.start()
    assert waiting.wait(10)  # A has committed (as "version 3") and is waiting for its verdict
    assert client.put(f"/api/plugins/artifact/artifact/{aid}", json={"code": "<p>B: throws</p>"}).json()["version"] == 3
    b = art._find(art._read_store(), aid)["versions"][-1]
    stamped = client.post(
        "/api/plugins/artifact/render-status",
        json={"id": aid, "version": 3, "ts": b["ts"], "ok": False, "error": "B's source threw"},
    )
    edit_a.join(10)
    assert stamped.json()["recorded"] is True
    assert "version 3" in out["reply"], out
    assert "B's source threw" not in out["reply"] and "FAILED" not in out["reply"], out


def test_a_verdict_posted_after_a_trim_shift_stamps_the_version_that_was_rendered(art, monkeypatch):
    """The panel renders the newest version (position 3 of 3) and, before its verdict POST lands, a
    commit at the cap shifts that version to position 2. The verdict must follow the version it was
    rendered from — by its ts — not stamp the new edit now sitting at position 3; and once the
    rendered version has been trimmed away entirely, it's dropped rather than pinned on a survivor."""
    clock = [art._now()]

    def tick():  # strictly increasing: two commits in one real millisecond would share a ts
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr(art._store, "_now", tick)
    aid = _at_the_cap(art, monkeypatch)
    client = _data_client(art)
    rendered = art._find(art._read_store(), aid)["versions"][-1]  # what the panel rendered: v3 @ 3
    art.update_artifact.invoke({"old_string": "v3<", "new_string": "v4<", "artifact_id": aid})
    r = client.post(
        "/api/plugins/artifact/render-status",
        json={"id": aid, "version": 3, "ts": rendered["ts"], "ok": False, "error": "v3 threw"},
    )
    assert r.json()["recorded"] is True
    vers = art._find(art._read_store(), aid)["versions"]
    assert vers[1]["code"] == "<p>v3</p>" and vers[1]["render"]["error"] == "v3 threw"
    assert "render" not in vers[2], "the verdict was stamped on the edit that shifted into the slot"
    for i in (5, 6):  # v3 is trimmed away
        art.update_artifact.invoke({"old_string": f"v{i - 1}<", "new_string": f"v{i}<", "artifact_id": aid})
    late = client.post(
        "/api/plugins/artifact/render-status",
        json={"id": aid, "version": 3, "ts": rendered["ts"], "ok": False, "error": "v3 threw"},
    )
    assert late.json()["recorded"] is False
    assert all("render" not in v for v in art._find(art._read_store(), aid)["versions"])


def test_a_same_millisecond_pair_across_a_trim_gets_the_right_verdict_or_none(art, monkeypatch):
    """Two commits in one millisecond share a ts, so ts alone can't say which of them the panel
    rendered once a trim has shifted positions. The shell also sends the lifetime number, and
    with (n, ts) the verdict lands on the version that was rendered — or, once that version is
    trimmed away, is dropped even though its same-ts twin survives."""
    aid = _at_the_cap(art, monkeypatch)  # [v1, v2, v3]
    client = _data_client(art)

    def codes():
        return [v["code"] for v in art._find(art._read_store(), aid)["versions"]]

    monkeypatch.setattr(art._store, "_now", lambda: 1_000_000)  # every commit from here: one millisecond
    art.rewrite_artifact.invoke({"code": "<p>A</p>", "artifact_id": aid})  # [v2, v3, A]
    a = art._find(art._read_store(), aid)
    rendered = {"version": 3, "n": a["version_count"], "ts": a["versions"][-1]["ts"]}  # the panel renders A @ 3
    art.rewrite_artifact.invoke({"code": "<p>B</p>", "artifact_id": aid})  # [v3, A, B] — A shifted to 2
    vers = art._find(art._read_store(), aid)["versions"]
    assert codes() == ["<p>v3</p>", "<p>A</p>", "<p>B</p>"]
    assert vers[1]["ts"] == vers[2]["ts"] == rendered["ts"]  # the ambiguity is real: ts can't tell A from B

    r = client.post("/api/plugins/artifact/render-status", json={"id": aid, **rendered, "ok": False, "error": "A threw"})
    assert r.json()["recorded"] is True
    vers = art._find(art._read_store(), aid)["versions"]
    assert vers[1]["code"] == "<p>A</p>" and vers[1]["render"]["error"] == "A threw"
    assert "render" not in vers[2], "A's verdict landed on B, the newer same-ts version"

    for code in ("<p>C</p>", "<p>D</p>"):  # trim A away, while its same-ts twin B survives
        art.rewrite_artifact.invoke({"code": code, "artifact_id": aid})
    assert codes() == ["<p>B</p>", "<p>C</p>", "<p>D</p>"]
    late = client.post("/api/plugins/artifact/render-status", json={"id": aid, **rendered, "ok": False, "error": "A threw"})
    assert late.json()["recorded"] is False
    assert all("render" not in v for v in art._find(art._read_store(), aid)["versions"])


def test_an_artifact_deleted_during_the_verdict_wait_returns_no_verdict(art, monkeypatch):
    """A DELETE landing while a create/edit waits for its render verdict must end the wait with
    no verdict — not an exception out of the tool that just succeeded."""
    client = _data_client(art)
    monkeypatch.setattr(art._render_status, "_RENDER_WAIT_MS", 5000)
    monkeypatch.setattr(art._render_status, "_LAST_POLL_TS", art._now())  # a panel is polling
    waiting = threading.Event()
    real_await = art._render_status._await_render

    def spy(*args, **kwargs):
        waiting.set()
        return real_await(*args, **kwargs)

    monkeypatch.setattr(art._render_status, "_await_render", spy)
    out: dict = {}

    def create():
        try:
            out["reply"] = art.show_artifact.invoke({"kind": "react", "code": "x"})
        except Exception as e:  # noqa: BLE001 — the regression surfaced as an AttributeError here
            out["error"] = repr(e)

    tool = threading.Thread(target=create)
    tool.start()
    assert waiting.wait(10)
    aid = art._read_store()["current"]
    assert client.delete(f"/api/plugins/artifact/artifact/{aid}").json()["deleted"] == aid
    tool.join(10)
    assert "error" not in out, out
    assert out["reply"].startswith("Created react artifact") and "render" not in out["reply"], out
    # and check_artifact's identity lookup copes with a missing artifact too
    assert art._render_status._version_render(None, 1, (1, 0)) is None


def test_the_panel_reports_which_version_it_rendered(art):
    """The shell sends the rendered version's identity (lifetime number + ts) with its verdict,
    and re-renders when the version in a slot changes — at the cap every new version lands in
    the SAME slot."""
    js = art._SHELL_JS
    assert "n:renderingN,ts:renderingTs" in js
    assert "renderingN=(a.version_count||a.versions.length)-a.versions.length+vi+1;" in js
    assert 'var key=a.id+"@"+vi+"@"+v.ts;' in js and "renderingTs=v.ts;" in js


def test_save_file_artifact_parses_the_file_outside_the_store_lock(art, monkeypatch, tmp_path):
    """Preview extraction (up to 50 PDF/DOCX pages) and thumbnailing touch no store state. Under a
    cross-process lock, doing them while holding it stalled every other writer in every process —
    so a concurrent writer must complete WHILE a slow extraction is still running."""
    seen: dict = {}
    writers: list[threading.Thread] = []
    real_extract, real_thumb = art._preview._extract_preview, art._preview._thumbnail

    def slow_extract(p, data, mime):
        seen["extract_depth"] = art._store._FILE_LOCK_DEPTH
        w = threading.Thread(target=lambda: seen.update(other=_show(art, "<p>meanwhile</p>")))
        writers.append(w)
        w.start()
        w.join(3)  # holding the lock, this writer can't finish until the save does
        seen["other_finished_during_extraction"] = not w.is_alive()
        return real_extract(p, data, mime)

    def thumb(data, mime):
        seen["thumb_depth"] = art._store._FILE_LOCK_DEPTH
        return real_thumb(data, mime)

    monkeypatch.setattr(art._preview, "_extract_preview", slow_extract)
    monkeypatch.setattr(art._preview, "_thumbnail", thumb)
    f = tmp_path / "report.txt"
    f.write_bytes(b"quarterly numbers")
    reply = art.save_file_artifact.invoke({"path": str(f), "title": "Report"})
    for w in writers:
        w.join(10)
    assert seen["extract_depth"] == 0 and seen["thumb_depth"] == 0, seen
    assert seen["other_finished_during_extraction"] is True, "a concurrent writer waited on the file parse"
    fid = reply.split("Saved file artifact ")[1].split(" ")[0]
    saved = art._find(art._read_store(), fid)
    assert saved["title"] == "Report" and saved["versions"][-1]["code"] == "quarterly numbers"
    assert saved["versions"][-1]["file"]["filename"] == "report.txt"
    assert art._blob_path(fid, saved["versions"][-1]["blob"]).read_bytes() == b"quarterly numbers"
    assert art._find(art._read_store(), seen["other"]) is not None


def test_the_store_lock_is_reentrant_and_released(art):
    """A locked path calling another locked path must not re-take (and so deadlock on) its own
    file lock, and the outermost exit must release it."""
    with art._store._store_lock():
        aid = _show(art, "<p>nested</p>")  # show → _show (serialized) → _write_store → _gc_blobs
        assert art._store._FILE_LOCK_DEPTH == 1
    assert art._store._FILE_LOCK_DEPTH == 0
    assert art._read_store()["current"] == aid
    assert art._store._lock_path().exists() and art._store._lock_path().parent == art._store_path().parent


class _FakeMsvcrt:
    """msvcrt's locking() contract: LK_NBLCK raises OSError(EACCES) while another process holds
    the region. Busy for the first ``busy`` attempts."""

    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, busy=0, fail_errno=None):
        self.busy, self.fail_errno, self.calls = busy, fail_errno, []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.fail_errno is not None:
            raise OSError(self.fail_errno, "not lockable")
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError(errno.EACCES, "locked by another process")


def _as_windows(monkeypatch, art, fake):
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(art._store, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(art._store, "_FILE_LOCK_POLL_S", 0)


def test_windows_branch_polls_a_busy_lock_rides_out_a_held_file_and_unlocks(art, monkeypatch):
    """The Windows code path's LOGIC, on every platform (the real msvcrt path runs in the
    cross-process tests on the Windows CI shards): a busy lock is polled, not failed; a replace
    refused because a reader holds history.json open is retried; the lock is released."""
    fake = _FakeMsvcrt(busy=2)
    _as_windows(monkeypatch, art, fake)
    real_replace, refused = os.replace, [2]

    def replace(src, dst):
        if refused[0]:
            refused[0] -= 1
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    aid = _show(art, "<p>windows</p>")
    assert art._read_store()["current"] == aid and refused == [0]
    lk, un = _FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK
    assert fake.calls == [(lk, 1), (lk, 1), (lk, 1), (un, 1)]


def test_a_refused_replace_is_not_retried_off_windows(art, monkeypatch, tmp_path):
    """POSIX replaces an open file fine, so a PermissionError there is real — surface it.
    Drives ``_replace`` directly: going through a store write would take the real lock down
    the simulated platform's branch (``fcntl`` doesn't exist on a Windows runner)."""
    monkeypatch.setattr(art._store, "sys", types.SimpleNamespace(platform="linux"))
    attempts = []

    def replace(src, dst):
        attempts.append(dst)
        raise PermissionError(errno.EACCES, "read-only")

    monkeypatch.setattr(os, "replace", replace)
    src = tmp_path / "new.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        art._store._replace(str(src), tmp_path / "history.json")
    assert len(attempts) == 1


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_a_denied_store_read_is_retried_on_windows_and_never_read_as_empty(art, monkeypatch, platform):
    """Every read-only path reads the store without the lock, and Windows refuses to open a file
    that a write is replacing at that moment (PermissionError, not a wait). Readers must ride that
    out on Windows — the panel's /history must not 500 because a write landed — and a denial must
    NEVER be read as an empty store (inside a read-modify-write that would wipe it). POSIX has no
    such transient, so it raises at once. The real sharing violation is exercised by the
    cross-process torn-reader test on the Windows CI shards."""
    aid = _show(art, "<p>x</p>")  # a real write, on this machine's real platform, before the fake one
    monkeypatch.setattr(art._store, "sys", types.SimpleNamespace(platform=platform))
    store_path, real_read_text, denied = art._store_path(), Path.read_text, [2]

    def read_text(self, *args, **kwargs):
        if self == store_path and denied[0]:
            denied[0] -= 1
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    if platform == "win32":
        assert art._read_store()["current"] == aid and denied == [0]
    else:
        with pytest.raises(PermissionError):
            art._read_store()
        assert denied == [1]


def test_an_unlockable_filesystem_degrades_to_the_thread_lock_not_a_failed_write(art, monkeypatch, caplog):
    """A lock the OS refuses outright (not merely busy) must not fail every store write: it
    degrades to in-process serialisation — the pre-lock behaviour — and says so once."""
    fake = _FakeMsvcrt(fail_errno=errno.EBADF)
    _as_windows(monkeypatch, art, fake)
    with caplog.at_level("WARNING", logger="protoagent.plugins.artifact"):
        a1, a2 = _show(art, "<p>1</p>"), _show(art, "<p>2</p>")
    assert [a["id"] for a in art._read_store()["artifacts"]] == [a2, a1]
    assert sum("cross-process lock" in r.getMessage() for r in caplog.records) == 1
    assert (_FakeMsvcrt.LK_UNLCK, 1) not in fake.calls  # nothing was locked, so nothing unlocked


def test_each_new_degrade_errno_is_warned_with_the_path_and_a_repeat_is_not_silent(art, monkeypatch, caplog):
    """A degrade is warned once per errno, naming the errno and the lock path — not once per
    process, which hid a DIFFERENT later failure (EBADF first, then ENOLCK on a remount) behind
    the first one. A repeat of a known errno still logs, at debug: the store keeps writing
    without cross-process exclusion, and that must never go quiet."""
    fake = _FakeMsvcrt(fail_errno=errno.EBADF)
    _as_windows(monkeypatch, art, fake)
    with caplog.at_level("DEBUG", logger="protoagent.plugins.artifact"):
        _show(art, "<p>1</p>")
        _show(art, "<p>2</p>")  # the same errno again
        fake.fail_errno = errno.ENOLCK
        _show(art, "<p>3</p>")  # a new one
    lock_path = str(art._store._lock_path())
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "cross-process lock" in r.getMessage()]
    assert len(warned) == 2, warned
    assert "EBADF" in warned[0] and "ENOLCK" in warned[1], warned
    assert all(lock_path in m for m in warned), warned
    repeats = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG" and "still unavailable" in r.getMessage()]
    assert len(repeats) == 1 and "EBADF" in repeats[0] and lock_path in repeats[0], repeats
    assert len(art._read_store()["artifacts"]) == 3  # every write still landed


# ── a holder that never lets go ──────────────────────────────────────────────────────

_BOUND_S = 1.5  # the writer's lock bound in the hung-holder test


def test_a_hung_holder_makes_a_writer_give_up_at_its_bound_and_change_nothing(art, procs):
    """A process wedged mid-save holds the store lock and never lets go. A writer in another
    process must not wait forever: it gives up at its bound with a clear "busy" reply naming the
    holder, and changes NOTHING. Degrading to "write anyway" would be worse than waiting: the
    holder's eventual write-back of its stale snapshot silently erases what the writer wrote."""
    before = _show(art, "<p>before</p>")
    holding, release = _CTX.Event(), _CTX.Event()
    holder = procs("hold_lock", {"holding": holding, "release": release, "max_hold_s": _START_S})
    writer = procs("show_timed", {"code": "<p>writer</p>"}, patch={"_LOCK_TIMEOUT_S": _BOUND_S})
    procs.wait_ready()
    holder.go.set()
    assert holding.wait(_START_S)  # the holder has the lock and has read the store
    writer.go.set()
    try:
        status, value = writer.out.get(timeout=_BOUND_S + 20)
    except queue.Empty:
        pytest.fail("the writer was still blocked on the hung holder long past its bound")
    finally:
        release.set()  # un-wedge the holder, which now writes back its snapshot
    assert status == "ok", value
    reply, waited = value
    assert reply.startswith("The artifact store is busy") and "Nothing was changed" in reply, reply
    assert f"pid {holder.proc.pid}" in reply, reply
    assert _BOUND_S <= waited < _BOUND_S + 15, waited
    assert holder.result() == holder.proc.pid
    store = art._read_store()
    assert {a["id"] for a in store["artifacts"]} == {"a-held", before}
    assert all(a["versions"][-1]["code"] != "<p>writer</p>" for a in store["artifacts"])


def test_a_panel_write_that_outwaits_its_bound_is_a_503_not_a_500(art, monkeypatch):
    """The panel's mutating routes turn a timed-out store lock into 503 + Retry-After — the
    store is busy, nothing changed, try again — rather than a 500 from an unhandled exception."""
    aid = _show(art, "<p>v1</p>")
    client = _data_client(art)
    monkeypatch.setattr(art._store, "_LOCK_TIMEOUT_S", 0.3)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with art._store._store_lock():
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(10)
        r = client.put(f"/api/plugins/artifact/artifact/{aid}", json={"code": "<p>v2</p>"})
    finally:
        release.set()
        holder.join(10)
    assert r.status_code == 503 and "busy" in r.json()["detail"], (r.status_code, r.text)
    assert r.headers.get("retry-after") == "5"
    assert [v["code"] for v in art._find(art._read_store(), aid)["versions"]] == ["<p>v1</p>"]


def test_a_slow_live_holder_keeps_exclusion_and_the_writer_waits_its_turn(art, procs):
    """The bound is for a holder that never lets go, not a slow one. A holder that takes a while
    (1 s, well inside the writer's 20 s bound) keeps exclusion: the writer waits for it, and both
    writes land — the writer's isn't erased by the holder's write-back."""
    holding, release = _CTX.Event(), _CTX.Event()
    holder = procs("hold_lock", {"holding": holding, "release": release, "max_hold_s": 1.0})
    writer = procs("show_timed", {"code": "<p>writer</p>"}, patch={"_LOCK_TIMEOUT_S": 20.0})
    procs.wait_ready()
    holder.go.set()
    assert holding.wait(_START_S)
    writer.go.set()
    reply, _waited = writer.result()
    assert reply.startswith("Created html artifact"), reply
    holder.result()
    store = art._read_store()
    assert "a-held" in {a["id"] for a in store["artifacts"]}
    assert any(a["versions"][-1]["code"] == "<p>writer</p>" for a in store["artifacts"]), "the writer's create was lost"


def test_a_dead_holder_leaves_no_stale_lock(art, procs):
    """The stale-holder story needs no lock-breaking: the OS lock belongs to the holder's open
    file, so a holder killed mid-save releases it with its process, and the next writer goes
    straight in — well inside its bound, without anyone deciding the holder was "stale"."""
    holding, release = _CTX.Event(), _CTX.Event()
    holder = procs("hold_lock", {"holding": holding, "release": release, "max_hold_s": _START_S})
    writer = procs("show_timed", {"code": "<p>writer</p>"}, patch={"_LOCK_TIMEOUT_S": 20.0})
    procs.wait_ready()
    holder.go.set()
    assert holding.wait(_START_S)
    holder.proc.kill()
    holder.proc.join(_START_S)
    writer.go.set()
    reply, waited = writer.result()
    assert reply.startswith("Created html artifact"), reply
    assert waited < 20.0
    assert "a-held" not in {a["id"] for a in art._read_store()["artifacts"]}  # the killed save wrote nothing


# ── a blob download racing the eviction sweep ───────────────────────────────────────────


def _saved_file_id(art, path):
    return art.save_file_artifact.invoke({"path": str(path)}).split("Saved file artifact ")[1].split(" ")[0]


def test_a_blob_evicted_mid_download_still_arrives_whole(art, procs, monkeypatch, tmp_path):
    """On a real socket: a file artifact's download has passed every check and is about to send
    when another PROCESS evicts the artifact and its sweep deletes the blob. The download must
    still arrive complete and byte-identical: it streams from a handle opened before sending.
    Served by path, the sweep landed between the headers and the open, and the client got a
    Content-Length and then a dropped connection. On Windows the sweep can't delete a file the
    download has open, so its store write must still succeed, and a later sweep removes it."""
    import socket

    import anyio
    import httpx
    import uvicorn
    from fastapi import FastAPI

    monkeypatch.setenv("ARTIFACT_HISTORY", "1")
    payload = os.urandom(3 * 1024 * 1024 + 123)
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    fid = _saved_file_id(art, src)
    blob = art._blob_path(fid, art._find(art._read_store(), fid)["versions"][-1]["blob"])

    at_start, swept = threading.Event(), threading.Event()
    app = FastAPI()
    app.include_router(art._build_data_router(), prefix="/api/plugins/artifact")

    async def pausing(scope, receive, send):
        async def paused_send(message):
            if message["type"] == "http.response.start" and scope["path"].endswith("/blob"):
                at_start.set()  # every check has passed; the body is next
                await anyio.to_thread.run_sync(swept.wait, _START_S)
            await send(message)

        await app(scope, receive, paused_send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(pausing, log_level="warning", lifespan="off"))
    serving = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    evictor = procs("show", {"code": "<p>evicts the file</p>"}, ARTIFACT_HISTORY="1")
    got: dict = {}

    def download():
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/plugins/artifact/artifact/{fid}/blob", timeout=_START_S)
            got.update(status=r.status_code, body=r.content)
        except Exception as e:  # noqa: BLE001 — a dropped connection is the failure being tested
            got.update(error=repr(e))

    serving.start()
    try:
        procs.wait_ready()
        deadline = time.monotonic() + _START_S
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        dl = threading.Thread(target=download)
        dl.start()
        assert at_start.wait(_START_S), "the download never reached its send"
        evictor.go.set()  # another process evicts the artifact now, its sweep deleting the blob
        assert "Created html artifact" in evictor.result()
        assert art._find(art._read_store(), fid) is None, "the file artifact wasn't evicted"
        if sys.platform != "win32":
            assert not blob.exists(), "the sweep didn't delete the blob mid-download"
        swept.set()
        dl.join(_START_S)
    finally:
        swept.set()
        server.should_exit = True
        serving.join(10)
    assert "error" not in got, got["error"]
    assert got["status"] == 200 and len(got["body"]) == len(payload)
    assert got["body"] == payload, "the download arrived, but not byte-identical"
    deadline = time.monotonic() + 10  # the download has let go: the next sweep takes the orphan
    while (art._blob_root() / fid).exists() and time.monotonic() < deadline:
        art._write_store(art._read_store())
        time.sleep(0.05)
    assert not (art._blob_root() / fid).exists()


@pytest.mark.parametrize("race", ["replaced", "deleted"])
def test_a_blob_swept_between_the_store_read_and_the_open_is_re_resolved(art, monkeypatch, tmp_path, race):
    """The blob route reads the store without the lock, so a write can orphan and sweep the blob
    it has just resolved before it opens it. "Latest" then follows the newer version that replaced
    it (not a 404 for a file that exists), and an artifact that's gone is a clean 404."""
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "1")
    src = tmp_path / "doc.txt"
    src.write_bytes(b"first")
    fid = _saved_file_id(art, src)
    client = _data_client(art)
    real_read, fired = art._store._read_store, []

    def racing_read():
        snapshot = real_read()
        if not fired:
            fired.append(race)
            if race == "replaced":
                src.write_bytes(b"second")
                art.save_file_artifact.invoke({"path": str(src), "artifact_id": fid})  # trims + sweeps v1's blob
            else:
                art.delete_artifact.invoke({"artifact_id": fid})
        return snapshot

    monkeypatch.setattr(art._store, "_read_store", racing_read)
    r = client.get(f"/api/plugins/artifact/artifact/{fid}/blob")
    assert fired == [race]
    if race == "replaced":
        assert r.status_code == 200 and r.content == b"second", (r.status_code, r.content)
    else:
        assert r.status_code == 404 and "unknown artifact" in r.text, (r.status_code, r.text)


def test_a_blob_the_sweep_cannot_delete_yet_is_skipped_alone_and_retried(art, monkeypatch):
    """Windows refuses to delete a file a download still has open (PermissionError). The sweep
    must skip only that file, so every other orphan still goes and the store write doesn't fail.
    The next sweep removes the skipped file once it's free."""
    d = art._blob_root() / "a-gone"
    d.mkdir(parents=True)
    for name in ("0-downloading.bin", "1-orphan.bin", "2-orphan.bin"):
        (d / name).write_bytes(b"x")
    in_use, held, real_unlink = d / "0-downloading.bin", [True], Path.unlink

    def unlink(self, missing_ok=False):
        if self == in_use and held[0]:
            raise PermissionError(errno.EACCES, "The process cannot access the file: it is being used")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    art._gc_blobs({"artifacts": []})
    assert sorted(p.name for p in d.iterdir()) == ["0-downloading.bin"]
    held[0] = False  # the download finished
    art._gc_blobs({"artifacts": []})
    assert not d.exists()
