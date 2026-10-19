# Documentation media workflow

Use [the generated inventory](../media-audit.md) to choose work. Edit
[`registry.json`](registry.json), the source of truth for media decisions,
production briefs, placements, and review history. GitHub tracks production
batches; the registry tracks individual assets. Do not maintain a second asset
spreadsheet in an issue body.

The baseline screen covers all 115 primary navigation pages and hubs. It
identifies 28 first-wave visuals, not 28 mandatory screenshots. Pages marked
`text` were screened for structure and task needs; that is not a claim that
readers can never benefit from a visual. `reuse` names a related owner instead
of commissioning the same screenshot twice. Revisit these decisions with
reader feedback and UI changes. Existing `docs/public/readme/` assets need a
current-UI review before reuse; none are silently certified by this audit.

## Choose the medium

- **Image:** a control, success state, or side-by-side comparison. Prefer a
  cropped PNG/WebP over a full desktop screenshot with unreadable labels.
- **Diagram:** relationships, boundaries, data lifetime, or ordering. Keep an
  editable source (for example `.drawio`, `.mmd`, or source SVG) and publish an
  SVG/PNG/WebP export. The surrounding prose remains the full explanation.
- **Video:** a short interaction where the transition matters. Prefer a short
  MP4/WebM with controls over an endlessly looping animation. Supply a still
  poster and `.txt` walkthrough; narrated video also requires English WebVTT
  captions. Add other languages when needed in a later schema revision.
- **Animation:** an existing GIF that is worth keeping. Render a still poster
  with an explicit link to open it. GIFs do not animate automatically in guides.

Do not make a visual the only place a required step, warning, command, or
success criterion appears. Alt text describes useful content; captions explain
why the reader should look. The seeded alt/caption fields are drafting prompts:
rewrite them against the actual asset during review.

## Stable slots and file ownership

A page contains a block with a durable ID, placed after the relevant procedure:

```html
<!-- docs-media: mcp-connect-test -->

<!-- /docs-media -->
```

The generator owns the block contents. A `planned`, `in-production`, `in-review`,
or `retired` record produces no visible output. There are no broken-image boxes
or “coming soon” panels. `ready` and `refresh-needed` retain the approved asset;
if it is misleading, retire it immediately instead of leaving it visible.

The ID describes the teaching purpose, not a release or file format. Reuse the
same ID in several pages by adding placements and marker pairs. Change the
asset once to update every placement. If the teaching purpose changes, create
a new ID. Do not add blocks to generated pages (navigation hubs or generated
API references): place the visual in the canonical task/explanation page, or
change that page's generator first.

Published files live under `docs/public/media/<id>/`, with revisioned filenames
such as `v1.webp`, `v2.mp4`, `v2-poster.webp`, `v2-steps.txt`, and `v2-en.vtt`.
Keep the ID stable while changing filenames to avoid stale browser caches.
Paths use letters, numbers, underscores, dots, slashes, and hyphens only.
Only currently referenced, reviewed exports belong in this directory. Remove
superseded exports in the same PR; Git retains history. Private drafts/raw
recordings stay outside `docs/public/` and outside the repository.

Store small editable diagram/annotation sources under `docs/dev/media/sources/`
or record a durable shared project URL in `source`. Do not record a private
local filesystem path that the next maintainer cannot access. Larger recordings
and editing projects can live in shared production storage; the approved
self-contained export still ships locally. Aim for stills below 500 KB and
short clips below 5 MB; larger assets need an explicit size/clarity tradeoff in
the PR because they increase desktop and wheel download sizes.

## Produce and publish one asset

1. Choose a P1 record and set its owner to the actual assignee, status to
   `in-production`, and optional `issue` to the production issue URL.
2. Follow its brief in an isolated demo instance, using synthetic data. Record
   the app commit/platform and capture settings in the editable project's notes.
   Never publish live tokens, usable pairing QR codes, private conversations,
   customer data, or personal paths. Do not fabricate a screenshot of a feature
   or success state that did not occur.
3. Stage drafts outside the public asset directory. Set `in-review`; review
   labels, crop/readability, light/dark contrast, accurate success state, alt
   text, and the text equivalent. For motion, check the poster and captions.
4. Copy approved exports to the ID's public directory. Set `asset`, and for
   motion `poster` and `transcript`. A video also needs `has_audio`; when true,
   set `captions` to a `.vtt` file. Set `source`, `reviewer`, `verified_on`
   (`YYYY-MM-DD`), and `verified_commit` (full app/source SHA). Set `ready`.
5. Generate and validate:

   ```bash
   python scripts/docs_media.py
   python scripts/docs_media.py --check
   python -m pytest tests/test_docs_media.py tests/test_docs_plugin.py -q
   npm run docs:build
   ```

6. Preview the target page on the website and in the app. Test both supported
   site bases (`/protoAgent/` and `/docs/`) when changing media rendering.
   The reusable browser check runs asset decoding, video playback, same-page image
   zoom, keyboard dismissal, and focus-return checks in Chromium and WebKit:

   ```bash
   node scripts/check_docs_media_browser.mjs http://localhost:4188/protoAgent/ http://localhost:7898
   ```

   Start a built-site preview and a fully isolated app from the same checkout first.
   Repeat with the `/docs/` base when changing media rendering. Images enlarge in
   the current page; Escape or Close returns focus to the image.

   On macOS, check actual native WKWebView decoding against the running app:

   ```bash
   swift scripts/check_docs_media_wkwebview.swift http://localhost:7898
   ```

   Check desktop playback when adding/changing video codecs: a successful site
   build does not prove WKWebView/WebView2 playback. Verify the transcript works
   even if the clip cannot play. Follow the normal repository gate/PR workflow.

Minimal publication fields, in addition to the existing brief/placement fields:

```json
{
  "status": "ready",
  "asset": "mcp-connect-test/v1.webp",
  "source": "docs/dev/media/sources/mcp-connect-test.svg",
  "reviewer": "actual-reviewer",
  "verified_on": "YYYY-MM-DD",
  "verified_commit": "full-40-character-source-commit"
}
```

This is a template, not a ready record. The validator rejects missing files,
missing review fields, unknown IDs, missing placements, unreviewed public files,
and incomplete motion alternatives. CI checks that generated blocks and the
inventory are current. It does not verify screenshot truth, visual quality, or
whether an arbitrary byte stream is actually a playable media file.

## Refresh or retire

When changing a relevant UI or contract, run:

```bash
python scripts/docs_media.py --changed-since origin/main
```

The report compares tracked changes (including the working tree) against each
record's `watch` paths. For a release review, compare against the asset's
`verified_commit` instead. It reports affected IDs; it does not automatically
claim an asset is stale or update its review date. Broad watched directories
produce candidates for human review, not proof of changed pixels. Inspect
untracked new UI sources separately.

For an asset needing a refresh, set `refresh-needed`, assign an owner, and keep
the last approved revision until the replacement passes review. Replace files
and update the review metadata in one PR; leave the slot ID and page prose alone.
If an image becomes inaccurate enough to mislead, set `retired`, remove its
exports, and regenerate. Keep its empty slots and record so the decision is
traceable. Review published assets at releases and at least every 90 days even
if no watched path changed; external UIs can drift outside this repository.

## Website, in-app help, and packaging

Generated blocks use ordinary HTML, not Vue-only components. VitePress resolves
`/media/…` under the configured site base. The bundled in-app reader rewrites
those URLs to `/plugins/docs/media/…`, which serves only the generated allowlist
from the bundled assets directory. Custom documentation roots keep their own
URLs and are never exposed through this route. The wheel and desktop sidecar
include only `docs/public/media/`, not the whole public directory or drafts.
The runtime reads no production briefs or private source projects.

Images are lazy-loaded. Video has controls, `preload="none"`, and no autoplay.
Local delivery supports offline reading; native playback support still depends
on the platform's codecs. No third-party player or tracking embed is required.
