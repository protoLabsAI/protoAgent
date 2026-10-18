# Marketing media

`plan.json` owns capture briefs and stable media IDs for the marketing refresh.
The product map is code in `src/components/ProductOverview.astro`; it is a conceptual
visual, not a captured interface. Planned product captures are not published or
replaced by synthetic screenshots.

When an asset is produced, record its editable source, exported file, exact tested
commit/date, reviewer, alt text and caption in its record. Put approved exports in
`public/media/<id>/`. Update the named page placement and reuse that ID for replacements.
For clips provide a poster, transcript, captions where speech conveys information,
and user controls. Verify desktop/mobile layout and actual playback before `ready`.

Review `watch` paths when the app changes. Set a published record to `refresh-needed`
if its UI or workflow no longer matches. Keep production work open until the capture
itself is checked; site builds and placeholder markers are not capture acceptance.

The social card uses the editable `social-card.html` source. Regenerate it with
`node scripts/render-marketing-social.mjs` from the repo root (root Node dependencies
and Playwright Chromium required). Check the output before committing it. This is
an authored brand graphic, not a product screenshot.

Approved marketing placements use `src/components/ProductMedia.astro` and the
registry asset paths. Replace a revision centrally; keep the on-page image zoom,
video controls, poster, and text walkthrough available. The first production
package and capture evidence are in [sources/2026-10-18](sources/2026-10-18/README.md).
