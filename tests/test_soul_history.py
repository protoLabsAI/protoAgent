"""SOUL.md version history (#1691) — write_soul archives the OUTGOING persona so prompt
iterations can be listed and restored. Isolation: PROTOAGENT_HOME → tmp box root (the autouse
conftest fixture re-resolves instance_paths per test)."""

from __future__ import annotations

from pathlib import Path

from graph import config_io


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    # Neutralize the bundled-seed fallback so these tests exercise ONLY the instance SOUL.md
    # (otherwise the first save would archive the repo's real seed persona). The seed-archival
    # behavior is covered explicitly by test_first_edit_archives_the_seed_persona.
    monkeypatch.setattr(config_io, "soul_source_path", lambda: tmp_path / "no-seed.md")
    return home


def _hist(home: Path) -> Path:
    return home / "config" / "soul-history"


def test_first_write_has_nothing_to_archive(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)  # seed neutralized → no prior persona at all
    config_io.write_soul("v1")
    assert (home / "config" / "SOUL.md").read_text() == "v1"
    assert config_io.list_soul_versions() == []  # nothing existed to snapshot


def test_first_edit_archives_the_seed_persona(monkeypatch, tmp_path: Path) -> None:
    # #1691 (steelman): the FIRST save overwrites the bundled seed the agent was actually
    # running on — that seed must be archived, or the default persona is lost the moment it's
    # edited. This is the case _home() deliberately neutralizes.
    home = tmp_path / "home"
    seed = tmp_path / "seed.md"
    seed.write_text("bundled default persona", encoding="utf-8")
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    monkeypatch.setattr(config_io, "soul_source_path", lambda: seed)

    config_io.write_soul("my custom persona")
    versions = config_io.list_soul_versions()
    assert len(versions) == 1
    assert config_io.read_soul_version(versions[0]["id"]) == "bundled default persona"


def test_write_snapshots_the_outgoing_version(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    config_io.write_soul("v1")
    config_io.write_soul("v2")  # archives v1
    versions = config_io.list_soul_versions()
    assert len(versions) == 1
    (only,) = versions
    assert config_io.read_soul_version(only["id"]) == "v1"  # the archived one is the OLD text
    assert (home / "config" / "SOUL.md").read_text() == "v2"  # live is the new text


def test_identical_consecutive_save_does_not_snapshot(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    config_io.write_soul("same")
    config_io.write_soul("same")  # old == new → nothing archived
    assert config_io.list_soul_versions() == []


def test_list_is_newest_first_with_metadata(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    for text in ("alpha", "beta persona text", "gamma"):
        config_io.write_soul(text)
    versions = config_io.list_soul_versions()
    # Three writes → two archived (alpha, beta); gamma is live.
    assert [config_io.read_soul_version(v["id"]) for v in versions] == ["beta persona text", "alpha"]
    newest = versions[0]
    assert newest["saved_at"].endswith("+00:00")  # ISO-8601 UTC
    assert newest["size"] == len("beta persona text")
    assert newest["preview"] == "beta persona text"
    # The live persona is "gamma" (not archived) → no archived version is flagged current.
    assert all(v["is_current"] is False for v in versions)


def test_is_current_flags_the_live_version_after_restore(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    config_io.write_soul("one")
    config_io.write_soul("two")  # archives "one"; live = "two"
    (v_one,) = config_io.list_soul_versions()
    assert v_one["is_current"] is False  # "one" is archived, "two" is live

    # Roll back to "one": it becomes live AND is present in history → flagged current.
    config_io.write_soul(config_io.read_soul_version(v_one["id"]))
    marked = [v for v in config_io.list_soul_versions() if v["is_current"]]
    assert [config_io.read_soul_version(v["id"]) for v in marked] == ["one"]


def test_is_current_marks_only_the_newest_of_identical_snapshots(monkeypatch, tmp_path: Path) -> None:
    # Non-consecutive identical saves BOTH land in history (dedup only collapses adjacent repeats),
    # so a persona can appear more than once. When the live persona equals them, only the newest
    # copy may be flagged current — else a restored-to persona lights up every identical row, which
    # is the "two rows say current" bug reported against #1691.
    _home(monkeypatch, tmp_path)
    for text in ("T", "U", "T", "U", "T"):  # archives T, U, T, U → history has T twice; live = "T"
        config_io.write_soul(text)
    versions = config_io.list_soul_versions()
    flagged = [v for v in versions if v["is_current"]]
    assert len(flagged) == 1, [(v["preview"], v["is_current"]) for v in versions]
    # ...and it is the NEWEST of the identical "T" snapshots (list is newest-first).
    t_rows = [v for v in versions if config_io.read_soul_version(v["id"]) == "T"]
    assert len(t_rows) == 2 and flagged[0]["id"] == t_rows[0]["id"]


def test_restore_roundtrip_is_itself_reversible(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    config_io.write_soul("original")
    config_io.write_soul("edited")  # archives "original"
    (orig_v,) = config_io.list_soul_versions()

    # "Roll back" to the archived original by re-saving it (what the restore route does).
    config_io.write_soul(config_io.read_soul_version(orig_v["id"]))
    assert (home / "config" / "SOUL.md").read_text() == "original"
    # Rolling back archived the version it replaced ("edited") — so it's reversible.
    assert "edited" in [config_io.read_soul_version(v["id"]) for v in config_io.list_soul_versions()]


def test_read_version_rejects_traversal_and_unknown(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    config_io.write_soul("a")
    config_io.write_soul("b")  # one real version exists
    assert config_io.read_soul_version("../../etc/passwd") is None
    assert config_io.read_soul_version("not-a-valid-id") is None
    assert config_io.read_soul_version("") is None


def test_history_is_capped(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(config_io, "_SOUL_HISTORY_CAP", 3)
    for i in range(6):
        config_io.write_soul(f"rev-{i}")  # each archives the previous → 5 snapshots attempted
    files = list(_hist(home).glob("*.md"))
    assert len(files) == 3  # pruned to the cap
    # The three kept are the most recent (rev-2, rev-3, rev-4 were the outgoing ones last kept).
    kept = sorted(f.read_text() for f in files)
    assert kept == ["rev-2", "rev-3", "rev-4"]
