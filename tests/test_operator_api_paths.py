from __future__ import annotations

import pytest

from operator_api.notes import NotesService
from operator_api.paths import resolve_project_path


def test_resolve_requires_non_empty_path() -> None:
    with pytest.raises(ValueError, match="required"):
        resolve_project_path("   ")


def test_resolve_rejects_missing_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        resolve_project_path(str(tmp_path / "nope"))


def test_resolve_rejects_file(tmp_path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_project_path(str(f))


def test_no_allowlist_is_permissive(tmp_path) -> None:
    # allowed_dirs=None keeps the pre-hardening behavior for non-operator callers.
    assert resolve_project_path(str(tmp_path)) == tmp_path.resolve()


def test_path_inside_allowed_root_is_accepted(tmp_path) -> None:
    root = tmp_path / "root"
    nested = root / "project"
    nested.mkdir(parents=True)

    assert resolve_project_path(str(nested), [str(root)]) == nested.resolve()
    # the root itself is allowed, not just descendants
    assert resolve_project_path(str(root), [str(root)]) == root.resolve()


def test_path_outside_allowed_root_is_rejected(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()

    with pytest.raises(ValueError, match="outside the allowed"):
        resolve_project_path(str(other), [str(allowed)])


def test_empty_allowlist_rejects_everything(tmp_path) -> None:
    # An empty list is still an allowlist (deny all), unlike None.
    with pytest.raises(ValueError, match="outside the allowed"):
        resolve_project_path(str(tmp_path), [])


def test_dotdot_traversal_cannot_escape_allowed_root(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    secret = tmp_path / "secret"
    allowed.mkdir()
    secret.mkdir()

    escape = str(allowed / ".." / "secret")
    with pytest.raises(ValueError, match="outside the allowed"):
        resolve_project_path(escape, [str(allowed)])


def test_symlink_cannot_escape_allowed_root(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "link"
    link.symlink_to(outside, target_is_directory=True)

    # The symlink lives inside the allowed root but points outside it;
    # resolve() follows it before the containment check, so it's rejected.
    with pytest.raises(ValueError, match="outside the allowed"):
        resolve_project_path(str(link), [str(allowed)])


def test_notes_service_enforces_allowlist(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    service = NotesService(allowed_dirs=lambda: [str(allowed)])

    # in-allowlist project saves/loads fine
    service.save_workspace(str(allowed), {"version": 1})
    assert service.load_workspace(str(allowed)) == {"version": 1}

    # out-of-allowlist project is blocked before touching disk
    with pytest.raises(ValueError, match="outside the allowed"):
        service.workspace_path(str(other))
