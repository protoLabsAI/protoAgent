---
name: docs-media
description: Use when auditing visual needs, planning screenshots, diagrams, GIFs or videos, or adding maintainable documentation media slots.
---

# Plan media that answers a reader's question

Screen every page in scope: recommend a visual, reuse an existing asset, or keep text,
with a reason. Prioritize unfamiliar UI, state transitions, spatial relationships, and
otherwise hard-to-recognize outcomes. A visual supplements an actionable text procedure.
Do not add decoration to meet a quota.

Choose a screenshot for recognition, a source-controlled diagram for relationships,
a short controlled video for motion, or an explicit-play GIF only when justified.
Prefer native diagram syntax where supported. Never fabricate product screenshots.

Maintain one authoritative registry using the repository's conventions. Give each asset
an immutable ID and record:

- purpose, audience, format, priority, status, owner;
- exact page and heading placements; existing asset/reuse relationship;
- capture brief: starting state, steps, framing, expected final state, safe sample data;
- source/editable project, published asset paths, alt text, caption;
- for motion: poster, transcript, captions when speech conveys information;
- reviewed version/commit, verification date, reviewer, watched source paths, refresh trigger.

Suggested lifecycle: planned → in production → ready → refresh needed → retired.
Keep planning artifacts out of the reader-facing page. If reusable slots are requested,
use stable IDs and one renderer/lookup path rather than scattering file names. Planned
slots render nothing; ready slots resolve to approved assets. Replacements retain the ID.
Retirement removes references deliberately. Detect missing files, duplicate IDs, unknown
slots, invalid status transitions, and stale source changes using the existing pipeline
or a small validator when implementation is in scope. No custom framework for a few images.

Validate site base paths, responsive layout, accessibility, and every publishing surface
(e.g. bundled/offline docs) that actually exists. Motion should have user controls and a
text alternative. Preserve editable sources and list the conditions that invalidate each
capture. Strip secrets and private paths before export. Separate metadata/URL checks from
real image inspection and playback. A placeholder is not a completed production asset.

Done when page coverage and production briefs are recorded; requested slots are validated;
and remaining capture work is owned or explicitly unassigned with concrete acceptance checks.
