# 0099 — Chat-bundle format v1: a structured, artifact-aware export for the P2 hosted viewer

Status: **Proposed**

## Context

P1 (#2158/#2181) exports a thread as a single self-contained Markdown string
(`graph/export_op.py::render_markdown`) — role headings, tool calls summarized as
name+args, artifacts excluded entirely (multi-part content becomes a `_[image_url]_`
placeholder). #2158's own design discussion named a structured "chat-bundle format" the
foundational piece still missing ("A. A chat-bundle format — new, foundational, currently
missing"), but P1 shipped redaction and the read-only-export shape instead, deferring the
format itself.

#2179 (P2, the hosted viewer at protolabs.studio) needs that format now — and needs it to
mirror the console's actual chat rendering (ordered text/tool-call parts, inline artifacts),
not a text dump a viewer can only display verbatim. A flat Markdown string can't drive that;
scoping #2179 split the work into six issues (#2680-#2685), of which this ADR covers the
first two: **#2680** (the format) and **#2681** (the builder that produces it).

Artifacts turned out to be the hard part. `plugins/artifact` stores each artifact as an
**instance-global, cross-thread version chain** — one JSON file, one `current` pointer for
the whole agent, no per-thread scoping and no per-message pointer at all. A chat message
only ever captures a *partial* trace of what happened to an artifact: `show_artifact` /
`rewrite_artifact` call-args carry full text, `update_artifact` carries only a diff,
`save_file_artifact` carries only a file path (bytes never appear in any message), and a
user's direct edit in the artifact panel leaves no message trace whatsoever. So "this
thread's artifacts" is not an authoritative set the graph state hands you — it has to be
*inferred* from tool-call results, and then a specific **version** of each has to be chosen,
correctly, for a bundle that's about to leave the machine on a public link.

## Decision

**D1 — Structured JSON, not a second Markdown flatten.** The bundle manifest mirrors the
console's own message model (`ChatMessage` / `ChatPart` / `ToolCall` in
`apps/web/src/lib/types.ts:567,609,722`): an ordered `messages[]` list, each with `role`
(`user`/`assistant`) and ordered `parts[]` of kind `text` or `tool_call`. A tool_call part
nests `{id, name, input, output}` together as one unit — matching `ChatMessage.toolCalls`
exactly — rather than the raw LangGraph shape, where a tool result is a separate message
entry. `bundle_version: 1` from the start. System messages are excluded, same rule as P1.

**D2 — Reuse P1's primitives; don't fork the redaction pass.** `graph/chat_bundle.py` is a
sibling to `graph/export_op.py`, not a replacement — `/export` still produces Markdown,
untouched. Three of `export_op`'s message-shape helpers were promoted from private to
shared (`role_of` / `text_of` / `tool_calls_of`, dropping their leading underscore) so both
walks use the same primitives rather than drift; `redact()` is imported and reused as-is,
and now also runs over inlined artifact content — a redaction surface P1 never had because
it never touched artifacts.

**D3 — Artifacts resolve through an injected callback, never a direct plugin import.**
Nothing in `graph/`, `server/`, or `operator_api/` imports a specific plugin today (verified
by grep — zero precedent either direction). `chat_bundle.build_bundle` takes an optional
`artifact_resolver: (id, version) -> dict` parameter; the real one
(`plugins.artifact.resolve_for_bundle`) is wired in defensively at the call site (intended:
`server/chat.py`, mirroring how it already orchestrates `export_thread`). This keeps the op
host-free and unit-testable with no plugin loaded (mirrors `export_op` / `snapshot_op`'s
"explicit inputs, no `STATE`" shape), and a caller with the plugin disabled gets
`available: false` parts instead of an `ImportError`.

**D4 — Which artifact VERSION gets bundled: exact index when safe, honest refusal when
not — never a guess.** `show_artifact` / `rewrite_artifact` call-args already carry the
full text for that exact turn, so those two read directly from the call, never touching the
version store (zero trim risk). `update_artifact` only has a diff, so it must consult
`plugins.artifact.resolve_for_bundle`, which indexes into the version chain by the number
the tool's result text reported (`"→ version N"` / `"→ vN"`). That number is trustworthy as
an index **only until the artifact's version count has ever exceeded its retention cap**:
`_write_store` trims from the front, and because the reported number is
`len(art["versions"])` taken *after* that commit's own trim, two different commits can
report the identical number once the chain sits at the cap — "version 2" becomes genuinely
ambiguous, not just possibly-evicted. Detected via a new `version_count` field (the lifetime
total, incremented before each trim — added to `plugins/artifact/__init__.py`'s stored
schema, backward-compatible/additive), **not** by comparing version timestamps: millisecond
timestamps can collide across rapid calls, which a real test caught as a false negative.
Once trimmed, every numbered lookup for that artifact returns `available: false` rather than
risk attaching the wrong revision to a public export.

**D5 — Binary (`file`-kind) artifacts are a placeholder in v1, not bytes.** Per the P2
scoping decision: a `file` artifact gets `{available: false, reason, file_meta: {filename,
mime, size}}`, never its blob (up to 25MB per the plugin's own cap) and never even its text
preview. Text/code/HTML/SVG/Mermaid/Markdown artifacts — what "mirror the console's
rendering" was actually about — inline fully.

**D6 — Packaging: a zip, like ADR 0091's snapshot bundle, not a bare `.json`.** `manifest.json`
+ `REVIEW.md` (the operator-facing disclosure — redactions found, artifacts not fully
included — written INSIDE the zip so it can never be separated from what it describes,
same reasoning as `snapshot_op.render_review` and P1's inline note). No per-artifact files
today, since inline text content already lives in `manifest.json` — the zip container is the
stable shape a future binary-attachment slice would add `artifacts/<id>-v<n>.<ext>` members
to, without another format break.

## Consequences

- `graph/chat_bundle.py` (`build_bundle`, `export_bundle`, `build_bundle_zip`) is a
  complete, tested unit with **no caller yet** — by design. #2682 (pre-publish review UI)
  and #2683 (publish client + wire contract) are separate issues that consume this; wiring
  a REST route was deliberately left to them rather than built prematurely here.
- `plugins/artifact/__init__.py` gained a public consumption seam
  (`resolve_for_bundle`) and a new stored field (`version_count`) — additive, safe for
  existing `history.json` files (old artifacts fall back to `len(versions)` until their next
  commit).
- Any future artifact-mutating tool must report its resulting version number in a way
  `chat_bundle`'s parser can find, or that call's artifact simply won't enrich the bundle
  (degrades gracefully — never crashes the export). A wording change in
  `plugins/artifact`'s result strings is a silent regression here worth a grep before
  shipping.

## Refs

#2179 (P2 parent) · #2680 (format) · #2681 (builder) · #2682 (pre-publish review UI) ·
#2683 (publish client + wire contract) · #2684 (revocation) · #2685 (hosted infra, separate
repo) · #2158/#2181 (P1) · ADR 0091 (agent-snapshot zip/manifest/REVIEW.md precedent).
