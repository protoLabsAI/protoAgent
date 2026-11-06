"""cite_artifact: cite a page of an ALREADY-saved PDF artifact, read-only (#4761).

It reuses card 1's one page-hint helper (``_ref.page_hint``) for every page rule, so a copied and
a linked artifact behave identically and the refusal/note text matches the save paths. It NEVER
writes the store, creates a version, or reads the source file.

Loaded as a package like test_artifact_linked.py, with the component validator registered so the
``artifact-ref`` chip encodes/decodes through graph.components."""

from __future__ import annotations

import copy

import pytest

from graph import components
from graph.components import extract_component, strip_component
from tests.test_artifact_linked import _arts, _fence, _load

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def art(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path / "store")
    components.set_plugin_components({mod._ref.ARTIFACT_REF: mod._ref.validate_artifact_ref})
    yield mod
    components.set_plugin_components(None)


def _save(art, monkeypatch, tmp_path, *, link=False, verdict=None, filename="doc.pdf", artifact_id="", title=""):
    """Save a file artifact (copied or linked) with a chosen pdf preview verdict and NO page hint,
    then return the stored artifact dict. The verdict stands in for the pdf.js preflight."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / filename).write_bytes(b"%PDF-1.4 fixture")
    _fence(monkeypatch, art, workspace=ws)
    monkeypatch.setattr(art._pdfview, "preflight", lambda data: verdict)
    monkeypatch.setattr(art._preview, "_extract_preview", lambda *a: "extracted text")
    monkeypatch.setattr(art._preview, "_thumbnail", lambda *a: None)
    args = {"path": filename, "project": "workspace", "link": link}
    if artifact_id:
        args["artifact_id"] = artifact_id
    if title:
        args["title"] = title
    out = art.save_file_artifact.invoke(args)
    assert ("Linked file artifact" if link else "Saved file artifact") in out
    return _arts(art)[0]


# ── r1: cite a copied AND a linked PDF → a chip for that artifact/version carrying page=N ──────


@pytest.mark.parametrize("link", [False, True], ids=["copied", "linked"])
def test_cite_returns_a_page_chip_with_the_helper_note(art, monkeypatch, tmp_path, link):
    saved = _save(art, monkeypatch, tmp_path, link=link, verdict={"render": True, "pages": 5}, title="Report")
    out = art.cite_artifact.invoke({"artifact_id": saved["id"], "page": 2})

    assert "Cited artifact" in out
    assert " — opens at page 2" in strip_component(out)  # the helper's note, verbatim
    comp = extract_component(out)
    assert comp["component"] == art._ref.ARTIFACT_REF
    assert comp["props"] == {
        "artifact_id": saved["id"], "version": 1, "versions_total": 1,
        "title": "Report", "kind": "file", "page": 2,
    }


# ── r2: cite never touches the store, and never reads the source file ─────────────────────────


def test_cite_leaves_the_store_byte_for_byte_identical(art, monkeypatch, tmp_path):
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})
    path = art._store_path()
    before_file = path.read_bytes()
    before = copy.deepcopy(art._read_store())  # artifacts, versions, current

    out = art.cite_artifact.invoke({"artifact_id": saved["id"], "page": 2})
    assert extract_component(out) is not None  # it did succeed

    assert art._read_store() == before
    assert path.read_bytes() == before_file  # the file itself was never rewritten


def test_cite_a_copied_artifact_after_its_source_file_is_gone(art, monkeypatch, tmp_path):
    saved = _save(art, monkeypatch, tmp_path, link=False, verdict={"render": True, "pages": 3})
    (tmp_path / "workspace" / "doc.pdf").unlink()  # the file a copied save read from is deleted

    out = art.cite_artifact.invoke({"artifact_id": saved["id"], "page": 3})
    assert " — opens at page 3" in strip_component(out)
    assert extract_component(out)["props"]["page"] == 3


# ── r3: page past the end / page < 0 / non-PDF / refused or missing verdict == the helper ─────


@pytest.mark.parametrize("link", [False, True], ids=["copied", "linked"])
@pytest.mark.parametrize(
    "filename,verdict,page",
    [
        ("doc.pdf", {"render": True, "pages": 3}, 9),  # past the end → clamps to the last page
        ("doc.pdf", {"render": True, "pages": 3}, -1),  # negative page → refusal
        ("doc.txt", None, 2),  # not a PDF → refusal
        ("doc.pdf", {"render": False, "reason": "the PDF is password-protected"}, 2),  # refused verdict
        ("doc.pdf", None, 2),  # missing verdict → refusal
        ("doc.pdf", {}, 2),  # empty verdict → refusal
    ],
)
def test_cite_outcome_is_exactly_the_helper_output(art, monkeypatch, tmp_path, link, filename, verdict, page):
    saved = _save(art, monkeypatch, tmp_path, link=link, verdict=verdict, filename=filename)
    # Card 1's helper, on this version's stored `file` meta and the artifact kind, is the oracle.
    exp_page, exp_refusal, exp_note = art._ref.page_hint(saved["versions"][-1].get("file"), saved["kind"], page)
    out = art.cite_artifact.invoke({"artifact_id": saved["id"], "page": page})

    if exp_refusal is not None:
        assert out == exp_refusal  # the helper's refusal, verbatim and whole
        assert extract_component(out) is None  # a refusal emits no chip
    else:
        assert exp_note and exp_note in strip_component(out)
        assert extract_component(out)["props"].get("page") == exp_page


# ── r4: unknown id, out-of-range version, version=0 → latest ──────────────────────────────────


def test_cite_unknown_id_points_to_list_artifacts(art, monkeypatch, tmp_path):
    out = art.cite_artifact.invoke({"artifact_id": "a-nope", "page": 1})
    assert "No artifact 'a-nope'" in out and "list_artifacts" in out
    assert extract_component(out) is None


def test_cite_out_of_range_version_names_the_count(art, monkeypatch, tmp_path):
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})  # one version
    out = art.cite_artifact.invoke({"artifact_id": saved["id"], "page": 1, "version": 5})
    assert "has 1 version" in out
    assert extract_component(out) is None
    # a negative version is out of range too (not treated as "the latest")
    assert "has 1 version" in art.cite_artifact.invoke({"artifact_id": saved["id"], "page": 1, "version": -1})


def test_cite_version_zero_is_the_latest_and_position_selects_an_earlier_one(art, monkeypatch, tmp_path):
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})
    art_id = saved["id"]
    _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3}, artifact_id=art_id)  # → v2
    stored = art._read_store()["artifacts"][0]
    assert len(stored["versions"]) == 2

    # version=0 cites the latest; its chip is the plain latest-version ref (existing-caller shape)
    latest = art.cite_artifact.invoke({"artifact_id": art_id, "page": 1, "version": 0})
    assert "v2" in strip_component(latest)
    assert extract_component(latest)["props"] == {
        "artifact_id": art_id, "version": 2, "versions_total": 2,
        "title": stored["title"], "kind": "file", "page": 1,
    }

    # an explicit 1-based position points the chip's lifetime version number at that version
    older = art.cite_artifact.invoke({"artifact_id": art_id, "page": 1, "version": 1})
    assert "v1" in strip_component(older)
    props = extract_component(older)["props"]
    assert props["version"] == 1 and props["versions_total"] == 2 and props["page"] == 1


# ── r5: registered, and existing-caller chip props unchanged ──────────────────────────────────


def test_cite_is_registered_in_all_imports_and_register(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path / "store")
    assert "cite_artifact" in mod.__all__
    assert mod.cite_artifact.name == "cite_artifact"
    names: list[str] = []

    class _Reg:
        def register_tool(self, t):
            names.append(t.name)

        def register_skill_dir(self, *a, **k):
            pass

        def register_router(self, *a, **k):
            pass

        def emit(self, *a, **k):
            pass

    mod.register(_Reg())
    assert "cite_artifact" in names


def test_version_selector_default_leaves_existing_props_unchanged(art, monkeypatch, tmp_path):
    """The new version_pos selector's default must be byte-for-byte the pre-#4761 latest-version
    props every create/revise tool already emits."""
    saved = _save(art, monkeypatch, tmp_path, verdict={"render": True, "pages": 3})
    # Omitting version_pos == pointing it at the latest position.
    assert art._ref.ref_props(saved) == art._ref.ref_props(saved, version_pos=1)
    assert art._ref.ref_tail(saved, page=2) == art._ref.ref_tail(saved, page=2, version_pos=1)
    # and that default is exactly the latest-version shape.
    assert art._ref.ref_props(saved) == {
        "artifact_id": saved["id"], "version": 1, "versions_total": 1,
        "title": saved["title"], "kind": "file",
    }
