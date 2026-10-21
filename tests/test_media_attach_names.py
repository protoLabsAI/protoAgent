"""Explicit attachment names distinguish otherwise identical content (#4274)."""

import pytest

from infra import media


@pytest.fixture(autouse=True)
def _turn(tmp_path, monkeypatch):
    import runtime.state as rs

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://agent.example")
    monkeypatch.setattr(rs.STATE, "graph_config", None)
    token = media.begin_turn_files()
    try:
        yield
    finally:
        media.end_turn_files(token)


@pytest.mark.parametrize("same_ref", [False, True])
def test_distinct_explicit_names_keep_identical_content(same_ref):
    first = media.save_media(b"same", "image/png")
    second = first if same_ref else media.save_media(b"same", "image/png")
    assert media.attach_file(first, name="a.png")
    assert media.attach_file(second, name="b.png")
    files = media.close_turn_files()
    assert [f["name"] for f in files] == ["a.png", "b.png"]
    assert f"/media/{first.path.name}?" in files[0]["url"]
    assert f"/media/{second.path.name}?" in files[1]["url"]


@pytest.mark.parametrize(
    ("first_name", "second_name"),
    [("", ""), ("", "a.png"), ("a.png", ""), ("a.png", "a.png")],
)
def test_matching_content_collapses_and_keeps_given_name(first_name, second_name):
    first = media.save_media(b"same", "image/png")
    second = media.save_media(b"same", "image/png")
    assert media.attach_file(first, name=first_name)
    assert media.attach_file(second, name=second_name)
    (file,) = media.turn_files()
    assert file["name"] == (second_name or first_name or second.path.name)
    assert f"/media/{second.path.name}?" in file["url"]


def test_same_explicit_name_replaces_different_content_without_merging_other_name():
    first = media.save_media(b"before", "image/png")
    second = media.save_media(b"after", "image/png")
    assert media.attach_file(first, name="a.png")
    assert media.attach_file(second, name="b.png")
    assert media.attach_file(second, name="a.png")
    files = media.turn_files()
    assert [f["name"] for f in files] == ["a.png", "b.png"]
    assert all(f"/media/{second.path.name}?" in f["url"] for f in files)


@pytest.mark.parametrize("names", [("a.png", "b.png"), ("b.png", "a.png")])
@pytest.mark.parametrize("same_ref", [False, True])
def test_unnamed_refresh_preserves_distinct_explicit_names(names, same_ref):
    first = media.save_media(b"same", "image/png")
    second = media.save_media(b"same", "image/png")
    assert media.attach_file(first, name=names[0])
    assert media.attach_file(second, name=names[1])
    for _ in range(2):
        refresh = first if same_ref else media.save_media(b"same", "image/png")
        assert media.attach_file(refresh)
        files = media.turn_files()
        assert [f["name"] for f in files] == list(names)
        assert f"/media/{refresh.path.name}?" in files[0]["url"]
        assert f"/media/{second.path.name}?" in files[1]["url"]
    # The refreshed entry still has an explicit identity for later replacements.
    replacement = media.save_media(b"changed", "image/png")
    assert media.attach_file(replacement, name=names[0])
    files = media.close_turn_files()
    assert [f["name"] for f in files] == list(names)
    assert f"/media/{replacement.path.name}?" in files[0]["url"]
    assert f"/media/{second.path.name}?" in files[1]["url"]
