"""Media publishing lifecycle, validation failures, and offline reader delivery."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

_load_docs = runpy.run_path("tests/test_docs_plugin.py")["_load_docs"]

API = runpy.run_path("scripts/docs_media.py")


@pytest.fixture
def media_repo(tmp_path):
    root = tmp_path
    for folder in ("docs/dev/media", "docs/guides", "docs/public/media", "plugins/docs", "src"):
        (root / folder).mkdir(parents=True)
    (root / "src/control.py").write_text("# UI source\n")
    (root / "docs/guides/example.md").write_text(
        "# Example\n\nText stays useful without media.\n\n<!-- docs-media: example -->\n\n<!-- /docs-media -->\n"
    )
    (root / "plugins/docs/catalog.json").write_text(
        json.dumps({"journeys": [{"landing": "guides/example.md", "groups": []}]})
    )
    item = {
        "priority": "P1",
        "kind": "image",
        "status": "planned",
        "owner": "Docs",
        "purpose": "Locate the control",
        "brief": "Capture the tested result",
        "alt": "The saved connection",
        "caption": "A saved connection",
        "refresh_when": "Control changes",
        "placements": [{"page": "guides/example.md", "heading": "# Example"}],
        "watch": ["src/control.py"],
    }
    data = {
        "version": 1,
        "baseline": "a" * 40,
        "pages": {"guides/example.md": {"decision": "media", "reason": "Locate the control"}},
        "media": {"example": item},
    }
    return root, data, item


def save(root, data):
    (root / API["REGISTRY"]).write_text(json.dumps(data))
    return API["load_registry"](root)


def publish(root, item, **extra):
    item.update(
        status="ready",
        asset="example/v1.webp",
        source="Editable capture project",
        reviewer="Docs reviewer",
        verified_on="2026-10-18",
        verified_commit="a" * 40,
        **extra,
    )
    (root / "docs/public/media/example").mkdir(exist_ok=True)
    (root / "docs/public/media" / item["asset"]).write_bytes(b"fixture")


def test_publish_replace_and_retire(media_repo):
    root, data, item = media_repo
    out = API["outputs"](save(root, data), root)
    assert "<figure" not in out["docs/guides/example.md"]
    publish(root, item)
    out = API["outputs"](save(root, data), root)
    assert 'src="/media/example/v1.webp"' in out["docs/guides/example.md"]
    assert json.loads(out[API["ASSETS"]])["files"] == ["example/v1.webp"]
    item["asset"] = "example/v2.webp"
    (root / "docs/public/media/example/v1.webp").rename(root / "docs/public/media/example/v2.webp")
    out = API["outputs"](save(root, data), root)
    assert "v2.webp" in out["docs/guides/example.md"] and "v1.webp" not in out["docs/guides/example.md"]
    item["status"] = "retired"
    (root / "docs/public/media/example/v2.webp").unlink()
    out = API["outputs"](save(root, data), root)
    assert "<figure" not in out["docs/guides/example.md"]
    assert "Text stays useful" in out["docs/guides/example.md"]


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda d: d["pages"].clear(), "every current"),
        (lambda d: d["media"]["example"].update(status="unknown"), "Invalid state"),
        (lambda d: d["media"]["example"].update(alt=""), "Missing alt"),
        (lambda d: d["media"]["example"]["placements"][0].update(heading="## Gone"), "heading changed"),
        (lambda d: d["media"]["example"].update(watch=["../secret"]), "watched source"),
    ],
)
def test_registry_rejects_drift(media_repo, change, match):
    root, data, _ = media_repo
    change(data)
    with pytest.raises(ValueError, match=match):
        save(root, data)


def test_unknown_duplicate_and_unclosed_slots(media_repo):
    root, data, _ = media_repo
    page = root / "docs/guides/example.md"
    original = page.read_text(encoding="utf-8")
    for text in (
        original.replace("example -->", "unknown -->"),
        original + original,
        original.replace("<!-- /docs-media -->", ""),
    ):
        page.write_text(text)
        with pytest.raises(ValueError):
            save(root, data)


def test_unreviewed_public_assets_and_missing_ready_asset_rejected(media_repo):
    root, data, item = media_repo
    (root / "docs/public/media/draft.png").write_bytes(b"private draft")
    with pytest.raises(ValueError, match="Unregistered"):
        save(root, data)
    (root / "docs/public/media/draft.png").unlink()
    publish(root, item)
    (root / "docs/public/media/example/v1.webp").unlink()
    with pytest.raises(ValueError, match="Missing asset"):
        save(root, data)


def test_motion_requires_accessible_alternatives(media_repo):
    root, data, item = media_repo
    publish(root, item)
    (root / "docs/public/media/example/v1.webp").unlink()
    item.update(
        kind="video",
        asset="example/v1.mp4",
        poster="example/poster.webp",
        transcript="example/steps.txt",
        has_audio=True,
        captions="example/en.vtt",
    )
    for name in ("v1.mp4", "poster.webp", "steps.txt", "en.vtt"):
        (root / "docs/public/media/example" / name).write_text("fixture")
    save(root, data)
    html = API["render"](item)
    assert 'controls playsinline preload="none"' in html
    assert '<track kind="captions"' in html
    assert "Read the text walkthrough" in html
    assert "autoplay" not in html
    (root / "docs/public/media/example/en.vtt").unlink()
    with pytest.raises(ValueError, match="Missing captions"):
        save(root, data)
    item.update(kind="animation", asset="example/v1.gif", has_audio=False)
    html = API["render"](item)
    assert '<img src="/media/example/poster.webp"' in html
    assert '<a href="/media/example/v1.gif"' in html
    assert '<img src="/media/example/v1.gif"' not in html


def test_caption_escaping_and_custom_reader_urls():
    docs = _load_docs()
    item = {
        "status": "ready",
        "kind": "image",
        "asset": "example/v1.webp",
        "alt": 'A "saved" <control>',
        "caption": "<script>not markup</script>",
    }
    md = API["render"](item)
    assert "<script>" not in md
    bundled = docs.render_markdown(md + '\n\n`src="/media/example/v1.webp"`', bundled_media=True)
    assert 'src="/plugins/docs/media/example/v1.webp"' in bundled
    assert "<code>src=&quot;/media/example/v1.webp&quot;</code>" in bundled
    assert 'src="/media/example/v1.webp"' in docs.render_markdown(md)


@pytest.mark.platform_sensitive
def test_media_route_exact_allowlist_and_containment(media_repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    root, _, _ = media_repo
    docs = _load_docs()
    media = importlib.import_module(docs.__name__ + ".media")
    monkeypatch.setattr(media, "__file__", str(root / "plugins/docs/media.py"))
    base = root / "docs/public/media"
    (base / "approved.webp").write_bytes(b"reviewed capture")
    (base / "draft.webp").write_bytes(b"not approved")
    (root / "plugins/docs/media-assets.json").write_text(json.dumps({"files": ["approved.webp", "../../secret.txt"]}))
    app = FastAPI()
    app.include_router(docs._build_view_router(), prefix="/plugins/docs")
    client = TestClient(app)
    response = client.get("/plugins/docs/media/approved.webp")
    assert response.status_code == 200 and response.content == b"reviewed capture"
    assert response.headers["content-type"] == "image/webp"
    assert client.get("/plugins/docs/media/draft.webp").status_code == 404
    assert media.media_file("../../secret.txt") is None
    try:
        (base / "escape.webp").symlink_to(root / "src/control.py")
    except OSError:
        return  # Windows without symlink privilege still exercises traversal above.
    (root / "plugins/docs/media-assets.json").write_text(json.dumps({"files": ["escape.webp"]}))
    assert media.media_file("escape.webp") is None


@pytest.mark.platform_sensitive
def test_committed_media_is_current():
    subprocess.run([sys.executable, "scripts/docs_media.py", "--check"], check=True)


def test_packaging_includes_only_media_subtree():
    assert '"docs/public/media": "docs/public/media"' in Path("hatch_build.py").read_text(encoding="utf-8")
    assert '("docs/public/media", "docs/public/media")' in Path("apps/desktop/sidecar/build_sidecar.py").read_text(
        encoding="utf-8"
    )
    assert '"docs/public": "docs/public"' not in Path("hatch_build.py").read_text(encoding="utf-8")


def test_ready_requires_review_metadata(media_repo):
    root, data, item = media_repo
    item["status"] = "ready"
    with pytest.raises(ValueError, match="Published media needs asset"):
        save(root, data)


@pytest.mark.platform_sensitive
def test_public_symlink_directory_is_rejected(media_repo):
    root, data, _ = media_repo
    try:
        (root / "docs/public/media/drafts").symlink_to(root / "src", target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="not symlinks"):
        save(root, data)


@pytest.mark.platform_sensitive
def test_reader_media_links_open_locally_for_root_and_fleet():
    """Execute the reader's URL and click paths, without a server or browser fixture."""
    import re
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to execute reader JavaScript")
    view = _load_docs()._VIEW_HTML
    helper = re.search(r"function localMediaHtml\(html\)\{.*?\n\}", view, re.S)[0]
    handler = re.search(r'\$reader.addEventListener\("click", async \(e\)=>\{(.*?)\n\}\);', view, re.S)[1]
    script = '''
const assert = require("node:assert/strict");
for (const base of ["", "/agents/demo"]) {
  HELPER
  const source = '<video poster="/plugins/docs/media/demo/still.webp"><source src="/plugins/docs/media/demo/clip.mp4"></video><a href="/plugins/docs/media/demo/steps.txt">Text</a>';
  const html = localMediaHtml(source);
  for (const file of ["still.webp", "clip.mp4", "steps.txt"]) assert.ok(html.includes(base+"/plugins/docs/media/demo/"+file));
  const $reader = {contains: () => true};
  const opened = [];
  const window = {open: (...args) => opened.push(args)};
  const candidatePaths = () => {throw new Error("Media must not be probed as a doc");};
  const click = async (e) => { HANDLER };
  const href = base+"/plugins/docs/media/demo/steps.txt";
  let prevented = false;
  await click({target: {closest: () => ({getAttribute: () => href})}, preventDefault: () => {prevented=true;}});
  assert.ok(prevented);
  assert.deepEqual(opened, [[href, "_blank", "noopener,noreferrer"]]);
}
'''.replace("HELPER", helper).replace("HANDLER", handler)
    subprocess.run([node, "-e", "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})"], check=True)
