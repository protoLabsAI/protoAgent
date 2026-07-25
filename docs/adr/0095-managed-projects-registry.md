# 0095 — Managed projects registry: one place to declare a project

Status: **Proposed**

## Context

"The projects this agent manages" is declared in **four** places in protoAgent, in four
different shapes, with no shared identity between them:

| Where | Shape | What it actually means |
| --- | --- | --- |
| `filesystem.projects` (ADR 0007) | `{name, path, write, no_delete}` | the fs **fence** — dirs the agent may read/write |
| `operator_project_dir` | one string | which dir the console calls "this project" (`server/__init__.py`) |
| `project_board.repo` / `base_branch` / `worktrees_root` | one repo per instance | the repo the board branches worktrees off |
| `github.default_repo` / `github.repos[]` | `owner/name` strings | the repo picker for `/issue` + the GitHub view |

Registering one repo means typing its path into a work-folder row, its `owner/name` into
the GitHub repo list, and its path *again* into `project_board.repo` — three boxes, no
cross-check that they refer to the same thing, and nothing that detects drift when one
changes. A project is a first-class thing an operator manages; it has no first-class place
to be declared.

This is a self-contained protoAgent problem. An earlier draft of this ADR framed it as
coordination with two sibling systems (protoWorkstacean's org registry and protoMaker's
consumption of it); **both are dead and abandoned**, so there is no external registry to
adopt, sync from, or stay compatible with. That simplifies the decision rather than
weakening it — there is exactly one system of record to design, and it is this one.

Two constraints carry over from the fence's history:

1. **The fence is an access grant, not a directory list.** ADR 0007 made
   `filesystem.projects` a small, explicit, operator-curated set precisely because
   membership means the agent can read and write there.
2. **A bad entry is silently catastrophic.** Per #2251, one unresolvable path skips at
   graph build, and if it was the only entry the *entire* fs toolset unbinds — the
   operator watches tools vanish with no stated cause. Whatever declares projects has to
   make a wrong entry visible.

## Decision

Introduce a first-class top-level **`projects:`** registry in `langgraph-config.yaml`.
It is the one place a project is declared; the existing keys become projections over it.

### D1 — The registry entry

```yaml
projects:
  - name: protoAgent                      # the identifier; fs tools already address projects by this
    path: /Users/kj/dev/protoAgent        # local checkout
    github: protoLabsAI/protoAgent        # for the GitHub repo picker
    default_branch: main                  # for the board's worktrees/PRs
    write: true                           # fence: read-write
    # no_delete: true                     # fence: create/edit, never delete
    # fs: false                           # registered, but NOT in the fs fence

  - name: github-plugin
    path: /Users/kj/dev/github-plugin
    github: protoLabsAI/github-plugin
    default_branch: main
    write: false                          # read-only
```

**One identifier, not two.** `filesystem.projects[].name` is already how the fs tools
address a project (`read_file(project="protoAgent", …)`, `list_projects`), so `name` is
the key and there is no separate `slug`. A distinct stable-id field would only earn its
keep as a join key against an external system, and there is none.

The registry describes a project's **binding on this host** — where it lives, what it is
on GitHub, what the agent may do to it. It deliberately holds no planning state (goals,
milestones, issue status); that lives in whatever board owns the work, which for this repo
is projectBoard-plugin's beads board.

### D2 — Existing config keys become projections; explicit values still win

- `filesystem.projects` ← entries where `fs != false`. Near-identity: `name` / `path` /
  `write` / `no_delete` are already the exact vocabulary.
- `github.repos` ← `entries[].github`, feeding the plugin's existing
  `effective_default_repo(default_repo, repos)` resolution unchanged.
- `project_board.repo` → a new `project_board.project: <name>` resolving path and base
  branch from the registry.

Non-regressing by construction: an explicitly configured `filesystem.projects` /
`github.repos` / `project_board.repo` continues to win over the derived value, so every
existing config and every fork loads unchanged and nothing migrates on upgrade.

The fence projection has one insertion point —
`LangGraphConfig.effective_filesystem_projects()`, which both production consumers
(`tools/fs_tools.py`, `graph/agent.py`) already route through. `scripts/gen_openshell_policy.py`
reads `filesystem_projects` directly and moves onto the same seam.

`operator_project_dir` is left alone. It is a single console pointer with its own
semantics (explicitly *not* an access grant), and folding it in would conflate "what the
console is looking at" with "what the agent may touch".

### D3 — Registered ⇒ fenced read-write by default

An entry grants fs access unless it opts out: `write: false` for read-only,
`no_delete: true` for create/edit-never-delete, `fs: false` for no fence membership at all.

This is the reverse of the cautious-looking default, and the reasoning is worth recording
because it is easy to re-litigate. The case for "membership grants nothing until
separately opted in" rests on entries arriving from somewhere other than the operator —
a sync feed, an org registry, an onboarding webhook. **No such source exists.** Every
entry is typed into this instance's own Settings, or picked with the ADR-0007 server-side
folder picker (#2264). Against that, a second opt-in for a directory you just
hand-registered buys no risk reduction, and it actively reproduces #2251 inverted:
register six projects, fence none of them, and the whole fs toolset unbinds with no
visible cause.

`fs: false` covers the genuine case — a project registered for GitHub/board purposes that
the agent should have no filesystem reach into.

**`fs: false` is honoured literally, including when every entry sets it** (amended after
review). A *configured* registry is the answer even when it projects to nothing: the fence
is empty and the filesystem toolset unbinds. The first implementation substituted the
default workspace there, reasoning that an empty fence unbinds everything with no visible
cause (constraint #2 above) — but that granted read-write on a directory the operator had
just declared out of reach, to fix a *visibility* problem with an *access* change. The
visibility problem is now fixed directly: every dropped or malformed entry logs a warning
naming it, and `GET /api/projects` reports `fence_source: "unbound"` with a console banner.
An *absent* registry still gets the workspace default — that is the default-install path
and is deliberately unchanged.

Should a population source ever be introduced, entries from it must land `fs: false` and
require explicit local promotion. That is a property of *imported* entries, not of the
registry.

### D4 — Wiring

`projects: list[dict]` on `LangGraphConfig`, parsed in `from_dict`, emitted as a top-level
key in `config_io.py` §B alongside `lifecycle_hooks` (both are lists of dicts, so neither
goes through the `string_list`-typed `FIELDS`). Golden map: one entry in
`tests/test_config_roundtrip.py`'s `FROM_YAML_EXAMPLE_FIELDS`, plus the list-of-dicts
allowlist next to the existing `filesystem_projects` note.

### D5 — API and console

`GET`/`POST /api/projects`. `/api/settings/filesystem-projects` is kept and becomes a
read-through projection, so the existing work-folder editor
(`apps/web/src/app/ToolsPanel.tsx`) keeps working while the richer panel lands.

The console gets a **Projects** area under Capabilities, reusing `PathPicker` (#2264) for
`path` — server-side browse is mandatory here for the reason that work records: the console
routinely configures a machine that is not the one running the browser, and neither
`webkitdirectory` nor `showDirectoryPicker()` can name a path on the server. Per constraint
#2 above, the panel shows each row's resulting access mode and flags a path that does not
resolve, so a wrong entry is visible at the moment it is made rather than as missing tools
later.

### D6 — Out of scope

- **Any external sync.** No source exists; designing a sync seam for one would be
  speculative.
- **A file-explorer / IDE console view.** Rendering a file tree and editor over the fenced
  projects is a plausible separate feature. It is not a prerequisite for this registry, and
  this registry is what would keep it from becoming a fifth place to declare a project.
- **Multi-host path mapping.** One host, one `path` per entry. A `paths: {host: path}` map
  is speculative until a second host needs it.

## Consequences

- One place to register a project; three consumers stop being hand-synced.
- The registry is a **capability** surface, not just metadata — adding an entry grants fs
  access by default (D3). The Settings panel must surface the resulting access mode per
  row, not bury it.
- `filesystem.projects` stays supported indefinitely as an explicit override, so the fence
  never depends on the registry being right.
- The registry deliberately cannot answer "what is the status of project X" — that is a
  board question, and keeping it out is what stops this from growing into a project
  management system.

## Alternatives rejected

**Extend `filesystem.projects` in place** with optional `github` / `default_branch` fields.
Materially cheaper — no new section, no migration, no golden churn beyond the added
optional keys. Rejected because the registry would then live *inside* the fs toolset's
config: an `fs: false` project sitting under `filesystem:` is incoherent, and
`filesystem_enabled: false` would have to stop disabling the list it contains.

**A plugin-owned registry.** Rejected on layering: the fence is core config consumed at
graph build, so a plugin-owned source of truth that core must obey at boot inverts the
import contract `lint-imports` enforces.

**Per-entry opt-in with an `access: none` default.** Rejected per D3 — its threat model
requires an entry source other than the operator, and the default reproduces #2251
inverted.

**Do nothing.** Defensible on cost: four boxes is annoying, not broken. Rejected because
the drift is silent — a moved checkout leaves `project_board.repo` pointing at the old
path while the fence points at the new one, and nothing reports the disagreement.
