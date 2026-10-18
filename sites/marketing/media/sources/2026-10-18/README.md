# Product captures — 2026-10-18

Brandlaunch produced these assets in Campaign Studio campaign 3,
`3-marketing-4339-real-product-captures`. Codex reviewed and integrated them for
[#4339](https://github.com/protoLabsAI/protoAgent/issues/4339). `exports.json`
maps the retained campaign originals to the public exports and their SHA-256 hashes.
The PNG exports are unchanged source captures; no UI or result text was redrawn.

## Capture environment

The app and web build used exact source `cd99084a79d375a45f5714286fc06d8735c25c7e`,
not an assumed current-main build. A disposable loopback app on port 8920 used its
own box root and fresh data. Real model turns used an existing native Claude
connection; no production conversations or credentials were copied. Researcher
and Editor were separate disposable members on ports 8921 and 8922.

Chromium/Playwright captured dark-theme views at 1440 CSS pixels wide and device
scale 2, with home-path/email/secret redaction enabled. The workspace was 900 CSS
pixels tall; memory and fleet were 1000. The attachment was the public
[README at 761b30f7](https://github.com/protoLabsAI/protoAgent/blob/761b30f74ed54fd3662d25dc3c8629b989dbbce3/README.md).

## Editable sources and original takes

The JSON scripts here are editable Campaign Studio script snapshots, not promises
that selectors and local state will survive later UI releases. The adjacent timing
logs record the actual takes. Absolute capture-machine paths in those logs have
been replaced with campaign-relative paths. Provision a fresh app, update the
script base URL, and stage the public document before replaying them.

| Export | Campaign source | Script |
| --- | --- | --- |
| Workspace | `shots/p1-final-20261018-211441/p1-final.mp4`, still 939 | 206 |
| First task | `shots/p1-real-document-20261018-210628/p1-real-document.mp4`, raw 934, export 942 | Original recipe below |
| Memory | `shots/memory-inspection-20261018-212324/memory-inspection.webm`, still 951 | 211 |
| Memory evidence | `shots/memory-source-turn-20261018-212218/memory-source-turn.webm`, stills 947–949 | 209 |
| Fleet | `shots/fleet-real-handoff-20261018-212544/fleet-real-handoff.mp4`, still 957 | 212 |

Raw video remains in Brandlaunch's retained campaign storage, not the public site.
The original PNGs are the public files. `memory-saved-preference.png` preserves the
matching source preference alongside the published injection-inspector image.
The original handoff is retained as Brandlaunch artifact `a-1792359172470-534711`.
An experimental annotation SVG was not used because its render was unverified.

The first-task export is source seconds **0.78–55**, scaled to 1920×1200 at 30fps,
H.264, with no bookends, interior cuts, audio, or speed changes. The poster is source
second 53. The original question was:

> Using only the attached public README, summarize the setup options in three bullets.
> Create a downloadable Markdown artifact titled “Setup options” containing those
> bullets and a source note. Do not install or run anything.

The script opened a new chat and the Artifact panel, attached the README, typed
that prompt, and selected Send. Its later verification wait targeted the wrong
iframe and timed out at approximately 193 seconds. That failure is retained in
the timing log; the published segment ends at 55 seconds, after the genuine result
and well before the failed wait. The current script 204 is a later revision and
is deliberately not represented as an exact archive of this original take.

The workspace still is a separate shorter-summary turn. The rejected 37-second
video from that turn showed an older result at the beginning; it is not published.
The selected 54-second take begins with an empty result pane.

## Review evidence and limits

- The attachment, file generation, and model wait are real. Brandlaunch verified
  HTTP 200 downloads with actual Markdown bytes: 1,239 bytes for the video's
  `setup-options.md` and 524 for the still's `setup-summary.md`. The video shows a
  downloadable result, not a completed save to the viewer's disk.
- The memory source and injection inspector contain the same preference:
  “Demo preference: Format task summaries as three short bullet points.” The
  subsequent question did not repeat it. This proves that turn's injection, not
  a guarantee that all saved memories reach every turn.
- The Researcher returned two supplied sample facts. Editor then returned their
  combined sentence. The selected still shows the completed return and both agents
  in the switcher; the earlier still showing work in progress is not published.
- Brandlaunch inspected the video throughout, and verified desktop playback and
  keyboard pause. Its mobile local-file test failed to locate a video, so Codex
  verified play, keyboard pause, seek/resume, and the walkthrough on the actual
  HTTP-served marketing page at 1440px and 390px before publication.
- Codex visually reviewed the selected PNGs, poster, and video frames. Integration
  checks and CI are recorded in the delivery PR. Browser viewport checks do not
  establish native mobile Safari or desktop-shell playback.

## Refresh and reuse

Update the existing IDs in `../../plan.json`, preserving the old revision until
the new capture is reviewed. Use `exports.json` to locate originals, the scripts
and timing logs to reconstruct the procedure, and the registry's watched paths
to identify UI changes. The silent walkthrough is versioned alongside the MP4.

These captures are candidates for docs backlog #4332: workspace/file tasks,
memory inspection, and fleet creation/handoff. The fleet switcher image is not a
Fleet Deck overview. Docs placement and acceptance remain separate work; no docs
record is marked ready by this marketing integration.
