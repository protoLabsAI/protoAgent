"""Concurrent first-use identity publication across real processes (#4197)."""
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.platform_sensitive


def _reader(path, barrier, output):
    from infra import paths

    target = Path(path)
    paths.instance_paths = lambda: SimpleNamespace(instance_uid_file=target)
    exists, read = Path.exists, Path.read_text
    first = True

    def raced_exists(self):
        nonlocal first
        result = exists(self)
        if self == target and first and not result:
            first = False
            barrier.wait(timeout=30)
        return result

    def raced_read(self, *args, **kwargs):
        nonlocal first
        result = read(self, *args, **kwargs)
        if self == target and first and not result.strip():
            first = False
            barrier.wait(timeout=30)
        return result

    Path.exists, Path.read_text = raced_exists, raced_read
    output.put(paths.instance_uid())


@pytest.mark.parametrize("empty", [False, True])
def test_first_readers_return_one_persisted_identity(tmp_path, empty):
    target = tmp_path / ".instance-uid"
    if empty:
        target.write_text("")
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(4), ctx.Queue()
    children = [ctx.Process(target=_reader, args=(str(target), barrier, output)) for _ in range(4)]
    try:
        for child in children:
            child.start()
        values = [output.get(timeout=40) for _ in children]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        assert len(set(values)) == 1
        assert len(values[0]) == 16
        assert values[0] == target.read_text()
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
        output.close()
        output.join_thread()


def test_existing_identity_does_not_need_write_access(tmp_path, monkeypatch):
    from infra import paths

    target = tmp_path / ".instance-uid"
    target.write_text(" existing-id\n")
    monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))
    monkeypatch.setattr(paths.os, "open", lambda *a, **k: pytest.fail("existing identity tried to acquire a write lock"))
    assert paths.instance_uid() == "existing-id"
    assert target.read_text() == " existing-id\n"


def _crash_during_publication(path):
    from infra import paths

    target = Path(path)
    paths.instance_paths = lambda: SimpleNamespace(instance_uid_file=target)
    replace = paths.os.replace

    def crash(source, dest):
        if Path(dest) == target:
            # atomic_write has flushed/closed the complete temporary file, but
            # publication has not happened. OS process exit must release the lock.
            os._exit(23)
        replace(source, dest)

    paths.os.replace = crash
    paths.instance_uid()


@pytest.mark.parametrize("empty", [False, True])
def test_interrupted_creator_leaves_no_partial_identity_and_next_process_recovers(tmp_path, monkeypatch, empty):
    from infra import paths

    target = tmp_path / ".instance-uid"
    if empty:
        target.write_text("")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_crash_during_publication, args=(str(target),))
    try:
        child.start()
        child.join(timeout=20)
        assert child.exitcode == 23
        assert not target.exists() or target.read_text() == ""
        monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))
        recovered = paths.instance_uid()
        assert len(recovered) == 16
        assert target.read_text() == recovered
        assert paths.instance_uid() == recovered
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)


def _hold_lock(path, ready, release):
    from infra.install_lock import _try_os_lock

    with open(path, "a+b") as lock:
        assert _try_os_lock(lock.fileno())
        ready.set()
        release.wait(timeout=30)


def test_wait_is_bounded_and_independent_roots_do_not_block(tmp_path, monkeypatch):
    import time
    from infra import paths

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    target = blocked / ".instance-uid"
    other = tmp_path / "independent" / ".instance-uid"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(target) + ".lock", ready, release))
    try:
        child.start()
        assert ready.wait(timeout=20)
        monkeypatch.setattr(paths, "_INSTANCE_UID_LOCK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))
        start = time.monotonic()
        assert paths.instance_uid() == ""
        assert time.monotonic() - start < 2
        assert not target.exists()
        monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=other))
        uid = paths.instance_uid()
        assert len(uid) == 16 and other.read_text() == uid
        release.set()
        child.join(timeout=10)
        assert child.exitcode == 0
        monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))
        assert len(paths.instance_uid()) == 16
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)


def test_unwritable_or_unlockable_root_never_returns_unpublished_identity(tmp_path, monkeypatch):
    from infra import paths, install_lock

    target = tmp_path / ".instance-uid"
    monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))

    def unavailable(*args):
        raise PermissionError("read-only root")

    monkeypatch.setattr(install_lock, "_try_os_lock", unavailable)
    assert paths.instance_uid() == ""
    assert not target.exists()


def test_publication_failure_releases_lock_and_does_not_invent_an_identity(tmp_path, monkeypatch):
    from infra import paths

    target = tmp_path / ".instance-uid"
    monkeypatch.setattr(paths, "instance_paths", lambda: SimpleNamespace(instance_uid_file=target))
    publish = paths.atomic_write

    def unavailable(*args):
        raise PermissionError("read-only root")

    monkeypatch.setattr(paths, "atomic_write", unavailable)
    assert paths.instance_uid() == ""
    assert not target.exists()
    monkeypatch.setattr(paths, "atomic_write", publish)
    assert len(paths.instance_uid()) == 16
