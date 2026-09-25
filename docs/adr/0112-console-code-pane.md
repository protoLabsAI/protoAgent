# 0112 — Console code pane: a read-only navigator surface the agent points into

- Status: Proposed
- Date: 2026-09-24
- Amended: 2026-09-25 — the code pane is an **opt-in toolset, off by default**
  (`filesystem.code_pane`); see [Amendment](#amendment-an-opt-in-toolset-off-by-default) (#3613)
- Implemented in: the server half (this PR): `tools/fs_secrets.py`, `tools/fs_view.py`,
  `tools/git_read.py`, `operator_api/browse_routes.py` (`GET /api/fs/file`,
  `GET /api/fs/diff`), `tools/fs_tools.py` (`show_code`), `graph/components.py`
  (`code-ref`). The console half ships in a separate PR against the contract below.
- Builds on: [ADR 0007](./0007-directory-aware-operator-agent.md) (the fs fence — every
  path here resolves through its `ProjectRegistry.resolve` chokepoint),
  [ADR 0051](./0051-a2a-realtime-streaming-and-component-rendering.md) (the
  `component-v1` DataPart relay `code-ref` rides, unchanged),
  [ADR 0035](./0035-console-layout-dual-rail-mobile-first.md) and
  [ADR 0056](./0056-unified-dockable-view-model.md) (dual-rail docks; the pane is a
  surface placed on the dock that is NOT holding chat),
  [ADR 0062](./0062-full-screen-document-viewer.md) (the module-level store + imperative
  `open*` pattern the pane's `codeviewer` store copies),
  [ADR 0086](./0086-chat-first-mobile-shell.md) (mobile: chat is the root, other
  surfaces are pushed — the pane never auto-opens there)

## Context

**The operator can't see what the agent is looking at.** An fs-enabled agent reads, greps
and edits files all turn long, but the console shows that work only as tool cards with
truncated previews. When the agent says "the bug is in the retry loop", the operator either
takes it on faith or leaves the console for an editor, finds the file, finds the line.
`open_in_editor` (#3597) and the fs-tool path links help only when the console and the agent
share a filesystem and the operator has a desktop editor — not for a remote fleet member,
not on the web console, not on a phone.

**Watching is not understanding.** Two recent studies make the design constraint concrete:

- Balepur et al., *(Im)Paired Programming: Coding Agents Improve Productivity but Harm
  Understanding* ([arXiv 2607.26375](https://arxiv.org/abs/2607.26375)): 54 students
  building websites with a coding agent vs a chatbot. The agent raised task completion but
  lowered comprehension; low-effort interaction (copy-paste prompts, auto-accepted edits)
  tracked the lowest understanding, and agent users were least prepared to extend the code
  themselves — while still preferring the agent.
- Anthropic, *How AI assistance impacts the formation of coding skills*
  ([anthropic.com/research/AI-assistance-coding-skills](https://www.anthropic.com/research/AI-assistance-coding-skills)):
  a randomized trial (52 developers learning a new Python library). The AI-assisted group
  scored ~17 points lower on comprehension; the high scorers were the ones who asked
  follow-ups and conceptual questions rather than delegating.

The lesson for a UI is not "show more of the agent's activity". A pane that auto-scrolls
through every file the agent touches turns the operator into a spectator, the
low-engagement mode both studies associate with the worst outcomes. The useful model is the
**navigator** of pair programming: the operator decides where to look and why; the agent
points at evidence *with a reason*, and the operator can go back to it.

**Two server-side hazards shape the API.** A pane that renders file content is a new
display surface (screen shares, screenshots pasted into issues), so a file whose name says
"credential" must not paint just because an agent pointed at it or a diff touched it. And
a diff view needs `git diff`, which is not a passive read: repository config and
`.gitattributes` can make it execute programs — external diff drivers, `textconv`, clean
filters, `core.fsmonitor`, hooks, submodule recursion
([Nesbitt, *Git diff drivers*](https://nesbitt.io/2026/03/30/git-diff-drivers.html)).
`.gitattributes` ships with a clone, and `.git/config` is one `write_file` away for an agent
with a writable project. Unhardened, "open the Diff tab" would give an agent's WRITE
permission a path to the server user's shell even with `filesystem.allow_run` off.

## Decision

### D1 — A read-only code pane is a core console surface

A new core surface `code` shows one file at a time (header: project · path, line range,
the agent's note, "Open in *editor*", copy path; a line-numbered, highlighted view with the
range highlighted and scrolled into view; a **Recent** trail of the last 20 refs to
revisit) with a **File | Diff** tab pair. It is read-only, full stop: no edit, no save, no
staging. Placement rule: it opens on the dock that does **not** hold chat, so pointing never
covers the conversation it came from. It is lazy-loaded.

### D2 — `GET /api/fs/file`: fenced, capped, line-addressed

`GET /api/fs/file?project=P&path=REL[&start=N&end=M]` resolves through
`tools.fs_tools.live_project_registry(cfg).resolve(project, path)`, the same fence as
`read_file` (`..`, absolute, `~` and symlinks out of the root are refused before any I/O),
and runs in `asyncio.to_thread`.

- **200** `{project, path, size, line_count, start, end, truncated, language, binary: false, text}`.
  `path` is the canonical project-relative path of the resolved target. `text` is lines
  `start..end` (1-based, inclusive), UTF-8 with replacement, line endings preserved (a CRLF
  file keeps its `\r\n`).
- **A line ends at `\n`** — what an editor gutter counts — not `str.splitlines`'s wider set
  (`\f`, a lone `\r`, `\x0b`, `\x1c`–`\x1e`, `\x85`, `\u2028`/`\u2029`). This PR moves
  `read_file` and `search_files` onto the same rule (`tools.fs_view.split_lines`; endings kept,
  CRLF still one line), so "line 42" means the same thing to `search_files`' `file:line`,
  `read_file(offset=)`, `show_code`, the pane and the operator's editor. Before, one form feed
  put `search_files`' hit a row below where the pane opened.
- **Caps**, streamed in fixed chunks so a 1 GB log or a one-line minified bundle costs
  bounded memory: 2 MB of returned text, 20,000 lines, 2,000 chars per line (the rest of a
  line is replaced by the marker ` … [line truncated]`, before its newline). At least one
  line always returns. `truncated: true` means a cap withheld part of what was asked for
  (the window, or through EOF when `end` is omitted); `end` is the last line returned, so the
  client pages with `start=end+1`.
- `language` is a Shiki language id guessed from the filename/extension (`"text"` fallback);
  the client may override it.
- **Binary** (a NUL in the first 8 KB, `_is_probably_binary`) → 200 with `binary: true`,
  `text: null`, `line_count`/`start`/`end` null.
- **Errors** are `{"detail": {"code", "reason"}}`: `bad_path` 400 (unknown project, fence
  escape — also what every project reports when `filesystem.enabled` is false), `denied`
  403 (D4), `not_found` 404, `not_a_file` 400 (directory, FIFO, socket, device — the read goes through
one `open_regular` descriptor, verified with `fstat`), `bad_range` 400 (`start` past
  EOF, `end < start`), `unreadable` 400 (other `OSError`).

### D3 — `GET /api/fs/diff`: hardened git, scoped to the project

`GET /api/fs/diff?project=P` → the working tree vs `HEAD`, for the Diff tab.

200 → `{project, is_git: true, head, branch, files, patch, truncated}`, each of `files`
being `{path, status, additions, deletions, binary, denied, old_path?, reason?, too_large?}`
(`reason` on denied entries; `too_large: true` on an untracked text file over 256 KB, whose
content is omitted); `status` is `M | A | D | R | ?`
(`?` = untracked; `old_path` only on `R`). Not a repository →
`{project, is_git: false, files: [], patch: ""}`. The patch is capped at 1 MB — streamed from git, which is killed
at the cap, so a changed multi-GB file is never buffered whole — cut on a line boundary,
`truncated: true`. A 10 s overall deadline → 504 `timeout`; other git failures →
400 `git_error`.

The git primitives live in a new `tools/git_read.py` (not `plugins.delegates`: core must not
import a plugin). Every invocation:

- runs a **fixed argv**, never caller options — the only paths are git's own output, passed
  back after `--` as `:(exclude,literal)` pathspecs;
- passes `--no-ext-diff --no-textconv --no-color --ignore-submodules=all`,
  `-c core.fsmonitor=false`, `-c diff.external=`, `-c core.hooksPath=<devnull>`,
  `-c core.pager=cat`, and **neutralizes every `filter.<name>` driver** the effective config
  defines (`-c filter.<name>.clean= … .smudge= … .process= … .required=false`) — there is no
  flag that turns filters off, and `git status`/`git diff` run a stat-dirty file's clean
  filter to hash it. The names come from `git config --get-regexp '^filter\.'`, which
  executes nothing;
- drops **every inherited `GIT_*` variable** (a superset of `GIT_DIR`/`GIT_WORK_TREE`/
  `GIT_INDEX_FILE`/`GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`, which also
  catches `GIT_EXTERNAL_DIFF` and `GIT_CONFIG_PARAMETERS`), then sets
  `GIT_OPTIONAL_LOCKS=0` (never write the index), `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`.

**Scoped to the fence, not the repository.** A project root can be a subdirectory of a
larger repo; `--relative` plus a `.` pathspec keep sibling changes out, and porcelain paths
(always repo-top-relative) are re-based on `git rev-parse --show-prefix`.

**Untracked files** come from `git status --porcelain=v1 -z --untracked-files=all`. Each
untracked text file ≤ 256 KB gets a synthetic `new file` patch built in Python — no
`git diff --no-index` (another git run over repository attributes, and one that can be aimed
outside the fence). A symlink is shown as git would record it (its target string, mode
`120000`) and never followed — unless it resolves outside the project or onto a
secret-like path, which `/api/fs/file` refuses: then the entry is `denied` (`reason`
"symlink outside the project" / "symlink to secret-like path") and its content omitted. The
same applies to a TRACKED symlink in the working tree, which is also excluded from git's patch. Larger files get a header only; binaries a
`Binary files … differ` line. Only regular files are read, through `tools.fs_view.open_regular` (`O_NONBLOCK | O_NOFOLLOW`,
then `fstat` must say `S_ISREG`, so a FIFO or symlink swapped in after the path checks is
refused rather than blocked on or followed), the untracked loop honors the deadline, and the file list stops at 5,000 entries
(`truncated: true`).

An unborn branch diffs against the empty tree (computed with `git hash-object -t tree`, so it
is right for SHA-1 and SHA-256 repos) and reports `head: null`.

`tests/test_fs_diff_route.py` proves this against a real repository armed with every vector
(`diff.<x>.command`, `diff.<x>.textconv`, `diff.external`, `filter.<x>.clean/smudge` with
`required`, `core.fsmonitor`, `core.pager`, a `post-index-change` hook), first checking that
stock git really does fire them there, then that the route fires none. Removing the filter
neutralization or the fsmonitor override fails it.

### D4 — A secret-like-name deny list for display surfaces

`tools/fs_secrets.py::is_secret_path(rel) -> str | None` (the reason) matches path
components case-insensitively, with `/` and `\` both separators: `.env`, `.env.*` except
`.env.example`/`.env.sample`/`.env.template`, `*.pem`, `*.key`, `*.p12`, `*.pfx`,
`*.keystore`, `id_rsa*`, `id_ed25519*`, anything under `.ssh/`, `secrets.yaml`,
`secrets.yml`, `.netrc`, `.npmrc`, `.pypirc`, `credentials*.json`.

- `/api/fs/file` checks it **before any existence check** (a 403/404 split would be an
  oracle for which secrets exist), on the requested name **and** on the resolved target's
  relative path (`notes.txt -> .env` is denied), and answers 403 `denied`.
- `/api/fs/diff` lists matching paths (either side of a rename) with `denied: true`, zeroed
  counts, and excludes them from the patch; untracked matches are never read.
- `show_code` refuses them (D5).

It is a name policy for **display** surfaces, not a content scanner (a key pasted into
`notes.txt` is not caught) and not an access control on the agent.

**Explicit non-goal: `read_file`'s own access is unchanged by this ADR.** Whether the agent's
fs tools should refuse or redact these names is a separate decision with its own costs (an
agent asked to fix a broken `.env` must be able to read it); it is a follow-up.

### D5 — `show_code` and the `code-ref` component

`show_code(project, path, line, end_line=None, note="")` is bound with the rest of the fs
toolset (read-only; works in `write: false` projects), next to `open_in_editor`. It validates
the fence, that the file exists, is not binary and not secret-like, `1 ≤ line ≤ line_count`,
`end_line ≥ line` (clamped to the file), and `note ≤ 280` chars. It returns a short text for
the model — `Showing <project>/<path>:<line>-<end_line> to the operator.` — followed by
`encode_component("code-ref", {project, path, line, end_line, note})`, which `server/chat.py`
lifts into a `component` frame exactly as it does for `show_component` (ADR 0051); the text
prefix becomes the tool card. The docstring tells the model to use it to point at evidence
**with a one-sentence why**, to prefer it over pasting large code blocks, and that it does not
read the file.

`code-ref` joins `graph/components.COMPONENT_TYPES` with a strict schema
(`validate_component_props`): exactly `project`/`path`/`note` strings (≤ 200 / 4096 / 280
chars, project and path non-empty) and integer `line ≥ 1`, `end_line ≥ line` — `bool` is
not an int here. Any other key (a smuggled `text`), type or length drops the whole component
at extraction. `show_component` refuses to build a `code-ref`; only `show_code` validates
the file first. A ref is a pointer, never content: the console fetches the text through the
fenced, deny-listed `/api/fs/file`, which is also why links work for **remote** fleet
members — no `/api/fs/roots` join, no shared filesystem.

### D6 — Console behavior (built in the console PR)

- **`code-ref` chip.** Renders in chat as a compact chip (`path:line-range` + note);
  clicking opens the pane. On the **live** `onComponent` path only — never on hydration or
  replay — a `code-ref` auto-opens the pane on desktop.
- **Mobile (ADR 0086): never auto-open.** The chip pushes the surface over chat when tapped.
- **Follow mode is opt-in** (a toggle in the pane header, default **off**, desktop only).
  When on, a completed `read_file`/`search_files`/`edit_file`/`write_file` with a parseable
  project+path on the LIVE tool-call path opens that file at `offset..offset+limit`, debounced
  (≥ 800 ms between jumps); a **pinned** state stops it moving away while the operator reads.
  Default-off is the research finding applied: pointing with a reason beats a moving camera.
- **Opening files.** An fs-tool path link opens the **pane** by default; ⌘/Ctrl-click and the
  header ↗ open the external editor. Settings "Open files in" becomes
  `protoAgent (default) | Zed | VS Code | Cursor | Off`.
- **Errors.** `denied` → "Hidden: secret-like file"; binary → metadata only; 404 → "file no
  longer exists".
- **Library.** See *Library decision* below.

### Library decision

**Accepted: `@pierre/diffs` 1.5.0, exact pin**, over reusing the DS's Shiki with a hand-rolled
renderer. The spike met every acceptance criterion:

| Criterion | Result |
|---|---|
| Follows light/dark | `theme: {dark: github-dark, light: github-light}`; `themeType` tracks the console mode live |
| Works inside our CSS | Renders in a Shadow DOM, styled through `--diffs-*` custom properties (console mono font, accent selection) |
| Scroll-to-line + range highlight | `selectedLines`; scrolling goes through the `Virtualizer` instance (a raw `scrollTop` write is undone by its anchor fix). e2e covers line 15,000 of a 20,000-line file |
| ≤ ~250 KB gz lazy chunk | `CodePane` chunk 105.1 KB gz; full static closure 169.8 KB gz, of which 66 KB is Shiki's JS regex engine shared with the chat markdown pipeline. Grammars and themes load on demand |
| No console errors | None from the pane, light or dark |

- **Main bundle:** 494.2 → 497.1 KB gz (+2.9 KB: store, chip, settings, routing).
- **Large files:** the `Virtualizer` plus `tokenizeMaxLength: 5000` brings a 20k-line file from
  7.4 s to ~0.45 s to first paint; files over 5,000 lines render as plain text.
- **One Shiki (the DS's 3.23).** pierre accepts `shiki ^3 || ^4`; left alone npm resolved 4.x
  beside the DS's 3.23 (via `@streamdown/code`) and Rollup emitted a second full set of grammar
  and theme chunks (`dist/assets` 14 MB → 25 MB). `apps/web` pins `shiki`, `@shikijs/themes`
  and `@shikijs/transformers` to 3.23.0 directly, the root `package.json` `overrides` pins
  pierre's copies to the same, and `vite.config.ts` adds `resolve.dedupe` for `shiki` and
  `@shikijs/*` (vitepress's Shiki 2 holds the root `node_modules` slot, so three physical 3.23
  copies still exist on disk). Result: `dist/assets` 15 MB. The Shiki 4 move is tracked
  upstream in protoContent#519.

## Amendment — an opt-in toolset, off by default (2026-09-25) {#amendment-an-opt-in-toolset-off-by-default}

v0.178.0 shipped the pane always-on. Josh, 2026-09-25: *"we need to not have the code and show
code stuff on by default"* — then, on how: *"it can be core and using native react, but it
should be … toggleable as a toolset."* So the pane stays core and native React (D1 — an iframe
plugin view would break the chat ↔ pane loop), and becomes a per-agent toolset like its
siblings under `filesystem.*`:

- **The switch is `filesystem.code_pane`** (bool, default `false`, needs `filesystem.enabled`),
  in **Settings ▸ Capabilities ▸ Tools ▸ Filesystem ▸ Shell & filesystem tools ▸ Code pane**.
  Per agent; hot-reloaded like the other fs toggles (a save
  rebuilds the graph). One predicate — `tools.fs_tools.code_pane_enabled(config)` — answers
  for every consumer below, so they can't disagree.
- **Off:** `show_code` is not built (the model never sees it, so no `code-ref` is emitted);
  `GET /api/fs/file` and `GET /api/fs/diff` answer **404 `{code: "disabled"}`** (checked
  against the live config per request, so a toggle needs no restart; the paths stay
  registered). `/api/fs/roots` and `/api/fs/browse` are unaffected — the external-editor links
  (#3596) and the path pickers use them. `show_component` still refuses `code-ref`.
- **The console learns the state from `/api/runtime/status` `code_pane.enabled`** — the status
  it already polls, per fleet window, so each member's window reads its own agent. A settings
  save invalidates that query, so the pane appears or disappears without a reload. Off, the
  console has **no Code surface** (rail, command palette, desktop launcher), **no "protoAgent"
  choice** under Settings ▸ Chat ▸ Open files in (a stored `protoagent` choice is kept but
  reads as the external editor — Zed by default — until the pane is back), fs path links are
  the plain editor links they were before the pane (#3596), **no follow mode**, and a `code-ref`
  replayed from history renders as inert text (`project/path:lines — note`). Unknown (before
  the first status, or an older server that doesn't report it) reads as off.
- **Unchanged when on:** everything in D1–D6.

The primitives stay core regardless of the toggle — `tools/fs_secrets.py`, `tools/fs_view.py`
(`read_file`/`search_files` use its `split_lines`) and `tools/git_read.py`.

## Consequences

- **The operator gets evidence, not a feed.** The agent has a cheap, validated way to put a
  line range and its reason in front of the operator, and the operator can revisit it. Whether
  it is used well is prompt/model behavior; the tool's docstring asks for the note.
- **New read surface over project files.** Bounded by the same fence as `read_file`, the
  existing `/api` operator auth, and D4 — but it is a new way file content leaves the server,
  so any widening (another content route, a write) needs its own decision.
- **Git hardening has a cost.** Neutralizing filter drivers means a stat-dirty file under a
  real filter (e.g. Git LFS) is compared by its raw bytes, so it can show as a modified
  binary until the index is refreshed by a normal `git status` elsewhere. Submodule changes
  are not shown. Both are accepted: the Diff tab is a view, not a source of truth.
- **The deny list will be wrong at the edges** — a project's own secret file with an unusual
  name renders; `id_rsa.pub` (public) is hidden. It is a small, conservative list in one module
  so it can be tuned in one place; content scanning is out of scope.
- **One line model.** `read_file`, `search_files`, `/api/fs/file` and `show_code` all end a
  line at `\n` (`tools.fs_view.split_lines`), so an agent's offsets, a `file:N` hit and the
  pane's gutter agree. `str.splitlines`' extra breaks (`\r`, `\f`, `\x85`, …) are not lines.
- **Revisit triggers:** the follow-up on `read_file` and the deny list (D4 non-goal); a
  second consumer of `tools/git_read.py` (it should then grow, not be forked); a Shiki 4 move
  in the DS (protoContent#519), which lets the console drop its 3.23 pin and overrides.

## Contract notes (server PR vs the shared draft)

Additions over the draft both PRs were built against, none of which change a field the
console reads:

- git hardening adds filter-driver neutralization, `core.hooksPath`, `core.pager`,
  `--ignore-submodules=all`, `-M`, and `--relative` + `.` scoping; the env scrub drops all
  `GIT_*`, not only the five named;
- error `detail` always carries `{code, reason}`, with codes `not_found`, `not_a_file`,
  `bad_range`, `unreadable`, `timeout`, `git_error` beside `bad_path`/`denied`;
- diff `files[]` entries may carry `old_path` (renames); denied entries report zero counts;
- a binary `/api/fs/file` answer has `line_count`/`start`/`end` null; `path` is the canonical
  resolved relative path;
- `show_component` refuses `code-ref`;
- **line numbering (integration finding):** `read_file` and `search_files` now count lines on
  `\n` only (D2), matching the pane — the one behavior change outside the new surfaces;
- diff entries carry `reason` when denied and `too_large: true` for untracked text files past
  256 KB; symlinks resolving outside the project or onto a secret-like path are denied (D3);
- a non-integer `start`/`end` is 400 `bad_range` (parsed by hand), not FastAPI's 422;
- `show_code`'s text part echoes the first and last line of the range
  (``… to the operator. L35: `if (…`  L38: `}` ``, each trimmed to ≤ 80 chars) so the model can
  check it pointed where it meant and re-point; its docstring sends it to `search_files`
  (`file:line`) or `read_file(offset=)` for exact numbers. `read_file`'s output is unchanged.

## References

- [arXiv 2607.26375](https://arxiv.org/abs/2607.26375) — Balepur, Baumler, Chen, Choi,
  Rudinger, Boyd-Graber, *(Im)Paired Programming: Coding Agents Improve Productivity but Harm
  Understanding*.
- [Anthropic — How AI assistance impacts the formation of coding skills](https://www.anthropic.com/research/AI-assistance-coding-skills).
- [Andrew Nesbitt — Git diff drivers](https://nesbitt.io/2026/03/30/git-diff-drivers.html).
- ADRs [0007](./0007-directory-aware-operator-agent.md), [0035](./0035-console-layout-dual-rail-mobile-first.md),
  [0051](./0051-a2a-realtime-streaming-and-component-rendering.md), [0056](./0056-unified-dockable-view-model.md),
  [0062](./0062-full-screen-document-viewer.md), [0086](./0086-chat-first-mobile-shell.md).
