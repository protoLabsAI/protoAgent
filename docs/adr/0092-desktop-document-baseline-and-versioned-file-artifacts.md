# 0092 — Desktop document-generation baseline + versioned file artifacts

Status: **Accepted** (phased — D1 ships with this ADR; D2/D3 follow in-track)

## Context

[ADR 0058](./0058-runtime-plugin-install-frozen-app.md) D2 gates plugin installs
in the frozen desktop sidecar: a plugin whose `requires_pip` entries aren't already
importable in the read-only PyInstaller bundle is refused at install time
(`graph/plugins/installer.py`) with *"install it on a server/Docker build instead."*
[#1953](https://github.com/protoLabsAI/protoAgent/issues/1953) / PR #1954 added an
**optional** tier (missing soft deps warn-and-continue), but that only degrades
gracefully — the feature still doesn't run on desktop.

Two real hits keep landing on this gate:

- **cowork** (the knowledge-work skill pack) declares `python-docx`, `openpyxl`,
  `python-pptx`, `reportlab` as **hard** `requires_pip`. On the desktop runtime the
  whole plugin is refused — so its docx/xlsx/pptx/pdf skills can't produce a file.
- **protobanana** is refused over `pillow>=10` (a soft dep it imports lazily).

The demographic protoAgent is courting here — the "own your stack" crowd migrating
off Claude Cowork / OpenAI Codex to a **local-first** agent they control — expects
that agent to actually produce documents. *"The desktop app can't make a .docx"* is
a credibility gap for that positioning, not a plugin edge case. cowork is a
**first-party flagship** pack, not an arbitrary third-party plugin, so it warrants a
platform answer rather than a per-plugin workaround.

There is a second, independent gap. The **artifact plugin** ([ADR 0038](./0038-generative-ui-artifacts-two-mode.md))
versions *renderable text source* — a version is a `code` string (HTML / Markdown /
SVG / React), diffed as text (`update_artifact` is an `old_string → new_string`
edit, capped at `max_code_kb`). It has **no bytes / mime / blob path**. So even once
the doc stack is bundled and cowork writes a real `.docx`, that file is a loose
binary in a scoped work folder with **no version history** — the artifact plugin
never sees it. For knowledge work, *the versioning has to hold for the files too.*

## Decision

### D1 — The desktop bundle baseline includes the document-generation stack *(ships with this ADR)*

Bake `python-docx`, `openpyxl`, `python-pptx`, `reportlab` (with `Pillow` + `lxml`
arriving transitively) into the frozen desktop app, **on by default**:

- A committed `apps/desktop/sidecar/requirements-docs.txt`, installed in the
  desktop-build CI sidecar step.
- A `DOC_COLLECT_ALL` list in `build_sidecar.py`, `find_spec`-guarded exactly like
  the existing Google `OPTIONAL_COLLECT_ALL` tier, so a lean local freeze without the
  extra still succeeds. `--collect-all` (not bare hidden-imports) is **required**:
  reportlab ships font/AFM data, python-docx/pptx ship their default `.docx`/`.pptx`
  templates as package data, and an import-scan misses all of it (the library raises
  at runtime opening its default template).

This does **not** contradict ADR 0058's rejection of shipping `git`+`pip` — it bakes
libraries at **build time**, which is precisely 0058's sanctioned mechanism (the same
way the `plugins/` tree and Google libs are bundled). It **expands the baseline**, it
does not add a runtime installer.

**Effect:** once these are importable in the bundle, `_deps_satisfied` short-circuits
and cowork's *hard* `requires_pip` is simply satisfied — cowork **installs and works
on desktop with zero manifest change**. protobanana's `pillow>=10` refusal also
disappears (Pillow is now bundled).

### D2 — A "download artifact" tier in the artifact plugin *(follow-up PR)*

Extend the artifact plugin so a version can carry **bytes + mime + a derived text
preview** in addition to (or instead of) the text `code`:

- Store the blob per version (for download) **and** extract a readable projection
  (`docx`→text, `xlsx`→sheet table, `pptx`→outline) so the panel renders an inline
  preview *and* the version history stays **diffable** — not an opaque blob.
- Render a **download card** for the binary rather than iframing it; existing text
  artifacts are untouched (purely additive `kind`).
- New config: max blob size + blob retention, alongside the existing `max_versions`.

### D3 — An explicit `save_file_artifact(path, title)` tool *(follow-up PR)*

The seam by which a generated file becomes a versioned artifact is an **explicit
tool** the skill calls after writing — deterministic, testable, agent-controlled, and
it works when there's no HTML source (cowork authors `.docx` directly via
python-docx, it doesn't render from an artifact). cowork's doc skills call it in
`~/dev/cowork-plugin` so their outputs land as versioned download-artifacts.
(Auto-watching a work folder was considered and deferred: it versions scratch files
too and needs watcher/debounce/dedup infra for no gain over the explicit call.)

## Consequences

- **Bundle size** grows ~15–25 MB (lxml, Pillow, reportlab binaries + fonts) on a
  desktop app that already ships a Python and a Node runtime — acceptable for the
  capability, and the driver is *what the demographic needs to work out of the box*.
- **Maintenance:** the doc stack becomes part of the maintained desktop baseline
  (security bumps, per-platform wheel availability — all four ship mac arm64/x86_64 +
  Windows wheels).
- **Verification:** the frozen sidecar smoke step (`scripts/live_smoke.py --bin`)
  should assert both that `import docx, openpyxl, pptx, reportlab` works **and** that
  `importlib.metadata.version("python-docx")` resolves in the *actual* frozen binary —
  the latter is what the D2 gate (`installer._importable`) checks first, and the dist≠import
  name gap (`python-docx`→`docx`) is why the build `--copy-metadata`'s each distribution
  alongside `--collect-all`. CI can only simulate the frozen path via `PROTOAGENT_PLUGIN_FROZEN=1`.
- **Scope boundary:** this is the *first-party flagship* answer. The **general**
  third-party dep story — installing *any* plugin's unbundled deps at runtime on the
  frozen app — stays the forthcoming **ADR 0093** (the pip-less wheel installer,
  [#1631](https://github.com/protoLabsAI/protoAgent/issues/1631) Scope A). 0092 and
  0093 are complementary: **bake** the flagship stack (0092), **install** arbitrary
  plugin deps at runtime (0093).

## References

- [ADR 0058](./0058-runtime-plugin-install-frozen-app.md) — the frozen D2 dep gate this expands
- [ADR 0038](./0038-generative-ui-artifacts-two-mode.md) — the artifact/generative-UI surface D2 extends
- ADR 0093 (forthcoming) — the complementary general pip path ([#1631](https://github.com/protoLabsAI/protoAgent/issues/1631) Scope A)
- [#1631](https://github.com/protoLabsAI/protoAgent/issues/1631), [#1953](https://github.com/protoLabsAI/protoAgent/issues/1953) / PR #1954 (optional tier)
- `apps/desktop/sidecar/build_sidecar.py`, `apps/desktop/sidecar/requirements-docs.txt`, `plugins/artifact/__init__.py`
- cowork-plugin, protobanana-plugin (motivating first-party / third-party cases)
