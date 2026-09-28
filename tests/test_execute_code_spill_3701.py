"""execute_code stdout spill (#3701 part 2/2).

When a script's stdout overflows ``output_truncate``, the head is returned inline
and the FULL output is written to a per-session scratch file under the plugin
store; the marker names the size + absolute path + how to read it. These run real
child processes (like tests/test_execute_code.py) with the plugin store redirected
to a tmp dir by monkeypatching ``graph.sdk.plugin_store``.
"""

import os
import time

import pytest

from plugins.execute_code import engine
from plugins.execute_code.engine import run_code


def _redirect_store(monkeypatch, root):
    """Point ``graph.sdk.plugin_store`` at ``root`` (created lazily), mirroring its
    real signature. Returns the spill dir the engine will use."""

    def _fake_store(subdir="", *, plugin_id):
        d = root / plugin_id / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("graph.sdk.plugin_store", _fake_store)
    return root / "execute_code" / "spill"


@pytest.mark.asyncio
async def test_overflow_returns_marker_and_spill_holds_full_stdout(tmp_path, monkeypatch):
    # r1: over the cap → truncated head + a marker naming the full size and spill
    # path, and the file at that path holds the COMPLETE stdout.
    spill_dir = _redirect_store(monkeypatch, tmp_path / "store")
    full = "A" * 2000 + "B" * 2000 + "C" * 1000  # 5000 distinct chars
    out = await run_code(f"print({full!r})", {}, truncate=100, session_id="sess-1")

    assert out.startswith("A" * 100)  # only the head is inline
    assert "of 5000 chars" in out  # full size reported
    assert "read_file" in out and "bounded read" in out  # how to read it

    files = list(spill_dir.glob("ec-*.txt"))
    assert len(files) == 1
    assert str(files[0]) in out  # the absolute spill path is named in the marker
    assert files[0].read_text() == full  # the file holds the complete stdout


@pytest.mark.asyncio
async def test_marker_recovery_read_returns_tail_not_head(tmp_path, monkeypatch):
    # Regression for the review finding: the marker's suggested recovery read must
    # return the part NOT already shown. A read from offset 0 would replay the same
    # head AND re-spill (its own stdout overflows ``truncate`` too). The marker now
    # directs a paged read starting at ``truncate`` — verify following it recovers
    # the tail and does not create a second spill.
    spill_dir = _redirect_store(monkeypatch, tmp_path / "store")
    full = "A" * 100 + "B" * 100 + "C" * 100  # three distinct 100-char windows
    truncate = 100
    out = await run_code(f"print({full!r})", {}, truncate=truncate, session_id="rec")

    # The marker pages from the head offset, not from the start.
    assert f".read()[{truncate}:" in out  # slice STARTS past the head
    assert ".read(200000)" not in out  # not the old head-replaying suggestion

    path = next(iter(spill_dir.glob("ec-*.txt")))
    # Follow the marker's own suggestion: read the next window at the same truncate.
    recovered = await run_code(
        f'print(open(r"{path}", encoding="utf-8", errors="replace").read()[{truncate}:{2 * truncate}])',
        {},
        truncate=truncate,
    )
    assert recovered == "B" * 100  # the tail window, not the "A"*100 head
    assert not recovered.startswith("A")
    assert "output truncated" not in recovered  # a full page didn't re-spill
    assert len(list(spill_dir.glob("ec-*.txt"))) == 1  # no second spill file created


@pytest.mark.asyncio
async def test_under_cap_creates_no_spill_and_output_unchanged(tmp_path, monkeypatch):
    # r2: under the cap → no spill file, return value identical to before.
    calls = {"n": 0}
    root = tmp_path / "store"

    def _fake_store(subdir="", *, plugin_id):
        calls["n"] += 1
        d = root / plugin_id / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("graph.sdk.plugin_store", _fake_store)

    out = await run_code("print('hello world')", {}, truncate=6000)
    assert out == "hello world"
    assert calls["n"] == 0  # the store was never touched
    assert not (root / "execute_code" / "spill").exists()


@pytest.mark.asyncio
async def test_spill_write_failure_falls_back_to_truncation_marker(monkeypatch):
    # r3: a broken store must fall back to the OLD truncation marker, not raise.
    def _boom(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("graph.sdk.plugin_store", _boom)

    out = await run_code("print('x' * 100)", {}, truncate=20)
    assert out.startswith("x" * 20)
    assert "truncated to 20 chars" in out  # the legacy marker
    assert "written to" not in out  # no spill path claimed


@pytest.mark.asyncio
async def test_old_spills_pruned_on_next_write(tmp_path, monkeypatch):
    # r4a: spill files older than 24h are pruned on the next spill; fresh ones survive.
    spill_dir = _redirect_store(monkeypatch, tmp_path / "store")
    spill_dir.mkdir(parents=True)

    stale = spill_dir / "ec-old-deadbeef.txt"
    stale.write_text("stale")
    old = time.time() - (25 * 60 * 60)
    os.utime(stale, (old, old))

    fresh = spill_dir / "ec-recent-cafef00d.txt"
    fresh.write_text("recent")  # mtime ~= now

    await run_code("print('y' * 500)", {}, truncate=50)

    assert not stale.exists()  # pruned (>24h)
    assert fresh.exists()  # kept (<24h)
    new = [p for p in spill_dir.glob("ec-*.txt") if p != fresh]
    assert len(new) == 1  # this run's spill was written


@pytest.mark.asyncio
async def test_single_spill_never_exceeds_size_cap(tmp_path, monkeypatch):
    # r4b: one spill file is capped, and the marker says the tail was dropped.
    spill_dir = _redirect_store(monkeypatch, tmp_path / "store")
    monkeypatch.setattr(engine, "_SPILL_MAX_BYTES", 500)

    out = await run_code("print('z' * 4000)", {}, truncate=50)

    files = list(spill_dir.glob("ec-*.txt"))
    assert len(files) == 1
    assert files[0].stat().st_size <= 500  # never exceeds the cap
    assert "capped at 500 bytes" in out  # and the marker admits it
