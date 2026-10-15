"""Failure injection against real SQLite and filesystem replacement boundaries."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from graph.skills import authoring
from graph.skills.index import SkillsIndex
from graph.skills.layered import LayeredSkillsIndex

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def skill(tmp_path):
    root = tmp_path / 'skills'
    idx = SkillsIndex(str(tmp_path / 'skills.db'))
    artifact = authoring.write_skill(root, 'Deploy', 'old', 'old body', provenance={'session_id': 'original'})
    idx.add_skill(artifact, source='disk')
    path = authoring.skill_path(root, 'Deploy')
    path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n') + b'<!-- exact -->\r\n')
    yield idx, root, idx.all_skills()[0], path.read_bytes()
    idx.close()


class FaultConnection:
    def __init__(self, conn, failure):
        self.conn, self.failure, self.fired = conn, failure, False

    def __getattr__(self, key):
        return getattr(self.conn, key)

    def execute(self, sql, args=()):
        if sql.startswith('UPDATE skills_fts') and self.failure in ('update_before', 'update_after') and not self.fired:
            self.fired = True
            if self.failure == 'update_after':
                self.conn.execute(sql, args)
            raise sqlite3.OperationalError('injected update failure')
        return self.conn.execute(sql, args)

    def commit(self):
        if self.failure in ('commit_before', 'commit_after') and not self.fired:
            self.fired = True
            if self.failure == 'commit_after':
                self.conn.commit()
            raise sqlite3.OperationalError('injected commit failure')
        return self.conn.commit()


@pytest.mark.parametrize('failure', ['write_before', 'write_after', 'update_before', 'update_after', 'commit_before', 'commit_after'])
@pytest.mark.parametrize('rename', [False, True])
def test_failed_replacement_restores_exact_state(skill, monkeypatch, failure, rename):
    idx, root, before, content = skill
    companion = root / 'deploy' / 'script.py'
    companion.write_text('asset')
    idx._conn = FaultConnection(idx._conn, failure)
    write = authoring.write_skill
    if failure.startswith('write'):
        def broken(*args, **kwargs):
            if failure == 'write_after':
                write(*args, **kwargs)
            raise OSError('injected write failure')
        monkeypatch.setattr(authoring, 'write_skill', broken)
    with pytest.raises(RuntimeError, match='prior state restored.*backup retained'):
        authoring.replace_skill(idx, root, before, 'Release' if rename else 'Deploy', 'new', 'new body')
    assert idx.all_skills() == [before]
    assert authoring.skill_path(root, 'Deploy').read_bytes() == content
    assert companion.read_text() == 'asset'
    assert not authoring.skill_path(root, 'Release').exists()
    assert idx.get_skill('Deploy')['prompt_template'] == before['prompt_template']


def test_recovery_failure_never_claims_restoration(skill, monkeypatch):
    idx, root, before, _ = skill
    idx._conn = FaultConnection(idx._conn, 'commit_before')
    copy = authoring._atomic_copy_file
    def broken(source, destination):
        if destination.name == 'SKILL.md':
            raise OSError('restoration denied')
        return copy(source, destination)
    monkeypatch.setattr(authoring, '_atomic_copy_file', broken)
    with pytest.raises(RuntimeError, match='artifact recovery unconfirmed.*backup retained') as exc:
        authoring.replace_skill(idx, root, before, 'Deploy', 'new', 'new body')
    assert 'prior state restored' not in str(exc.value)
    assert idx.all_skills() == [before]


def test_failed_learned_materialization_preserves_absence(skill):
    idx, root, before, _ = skill
    authoring.skill_path(root, 'Deploy').unlink()
    idx._conn.execute("UPDATE skills_fts SET source = 'emitted', confidence = 0.4")
    idx._conn.commit()
    before = idx.all_skills()[0]
    idx._conn = FaultConnection(idx._conn, 'commit_after')
    with pytest.raises(RuntimeError, match='prior state restored'):
        authoring.replace_skill(idx, root, before, 'Deploy', 'new', 'new body')
    assert not authoring.skill_path(root, 'Deploy').exists()
    assert idx.all_skills() == [before]


def test_layered_rename_preserves_identity_assets_and_commons(skill, tmp_path):
    private, root, before, _ = skill
    commons = SkillsIndex(str(tmp_path / 'commons.db'))
    layered = LayeredSkillsIndex(private, commons)
    companion = root / 'deploy' / 'script.py'
    companion.write_text('asset')
    try:
        current = layered.all_skills()[0]
        authoring.replace_skill(layered, root, current, 'Release', 'new', 'new body')
        row = private.all_skills()[0]
        assert row['id'] == before['id']
        assert row['created_at'] == before['created_at']
        assert row['name'] == 'Release'
        assert (root / 'release' / 'script.py').read_text() == 'asset'
        assert commons.all_skills() == []
        assert layered.delete_skill(row['id']) is True
    finally:
        commons.close()


def test_stale_concurrent_edit_cannot_overwrite_winner(skill, monkeypatch):
    idx, root, before, _ = skill
    entered, release = Event(), Event()
    write = authoring.write_skill
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return write(*args, **kwargs)
    monkeypatch.setattr(authoring, 'write_skill', held)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(authoring.replace_skill, idx, root, before, 'Deploy', 'first', 'first body')
        assert entered.wait(5)
        second = pool.submit(authoring.replace_skill, idx, root, before, 'Deploy', 'second', 'second body')
        release.set()
        first.result()
        with pytest.raises(RuntimeError, match='changed since it was read'):
            second.result()
    assert idx.get_skill('Deploy')['description'] == 'first'
    assert 'first body' in authoring.skill_path(root, 'Deploy').read_text()


def test_failed_archive_does_not_modify_live_state(skill, monkeypatch):
    idx, root, before, content = skill
    def broken(*args, **kwargs):
        raise OSError('archive denied')
    monkeypatch.setattr(authoring, 'archive_skill', broken)
    with pytest.raises(RuntimeError, match='no completed backup'):
        authoring.replace_skill(idx, root, before, 'Deploy', 'new', 'new body')
    assert idx.all_skills() == [before]
    assert authoring.skill_path(root, 'Deploy').read_bytes() == content


def test_rename_error_after_move_is_recovered(skill, monkeypatch):
    from pathlib import Path
    idx, root, before, content = skill
    rename = Path.rename
    def moved_then_failed(path, target):
        result = rename(path, target)
        if path == root / 'deploy':
            raise OSError('error after rename')
        return result
    monkeypatch.setattr(Path, 'rename', moved_then_failed)
    with pytest.raises(RuntimeError, match='prior state restored'):
        authoring.replace_skill(idx, root, before, 'Release', 'new', 'new body')
    assert idx.all_skills() == [before]
    assert authoring.skill_path(root, 'Deploy').read_bytes() == content
    assert not (root / 'release').exists()


def test_index_recovery_error_is_reported(skill):
    idx, root, before, content = skill
    class FailedRecovery(FaultConnection):
        def rollback(self):
            raise sqlite3.OperationalError('rollback denied')
    idx._conn = FailedRecovery(idx._conn, 'commit_before')
    with pytest.raises(RuntimeError, match='index recovery unconfirmed.*backup retained') as exc:
        authoring.replace_skill(idx, root, before, 'Deploy', 'new', 'new body')
    assert 'prior state restored' not in str(exc.value)
    assert authoring.skill_path(root, 'Deploy').read_bytes() == content
    idx._conn.conn.rollback()
