---
name: repo-onboard
description: Get the OPERATOR up to speed in an unfamiliar repository fast — clone or register it, find its manifest(s) and README and put them in front of the operator (the README as a markdown artifact, the manifest in the code pane), draw a code-linked Mermaid architecture overview, prove the toolchain, write a short repo card, then offer a guided tour one stop per turn. Use when the operator points you at a new repo or directory, says "get up to speed", "onboard", "what is this codebase", "how does this project fit together", "show me the readme", "show me the package.json", or before debugging code you haven't mapped yet.
---

# Repo onboard

Goal: in a few minutes the **operator** knows what the code is, how it runs, and where to
look, well enough to explain it. Steps 1–7 are plumbing: do them in **one turn**, then stop
with a choice. The tour (step 8) is interactive: one stop per turn. Cite `path:line`.
**Never change code during onboarding** — no edits, no writes, no commits.

**Read with the filesystem tools** — `list_dir`, `find_files`, `search_files`, `read_file` —
not `execute_code` or ad-hoc scripts: their `file:line` output is what `show_code` and the
diagram links need, and they stay inside the project fence. Setup commands go through
`run_command`. Every onboarding produces all three views: the README artifact, the manifest
in the code pane, and the overview diagram.
If those tools aren't bound ("`list_dir` is not a valid tool"), tell the operator first —
the filesystem toolset is off or a registered work folder is missing, and the server log's
`[fs]` line names it — then carry on with what you have, citing `path:line` in text.

## 1. Register it
- A git URL or `owner/repo` → `onboard_project(github_repo=..., write=true)`. It clones into
  the onboarding root (or reuses an existing checkout there and reports drift). Use the
  returned project name in every filesystem tool call.
- A folder already on disk → `register_local_project(path=...)`.
- Already registered → `list_projects` and move on.
- A refusal names the bound (the root or the allowed sources). Tell the operator which
  setting to change; don't work around it.

## 2. Find the manifest(s) and the README
`list_dir` the root, then identify the ecosystem(s) from what is actually there:

| Root file | Ecosystem · what to note |
| --- | --- |
| `package.json` | Node/TS · `scripts`, `main`/`bin`/`exports`, deps. Package manager from the lockfile: `package-lock.json` npm, `pnpm-lock.yaml` pnpm, `yarn.lock` yarn, `bun.lock(b)` bun |
| `pnpm-workspace.yaml`, `turbo.json`, `nx.json`, `lerna.json`, `"workspaces"` | JS monorepo · list the packages |
| `pyproject.toml`, `setup.cfg`/`setup.py`, `requirements*.txt` (+ `uv.lock`/`poetry.lock`) | Python · `[project.scripts]`, deps, tool config |
| `Cargo.toml` | Rust · `[workspace] members`, `[[bin]]` |
| `go.mod` | Go · module path; `cmd/*` are the binaries |
| `Gemfile` · `composer.json` · `mix.exs` · `deno.json(c)` | Ruby · PHP · Elixir · Deno |
| `pom.xml`, `build.gradle(.kts)` · `*.csproj`/`*.sln` | JVM · .NET |
| `mise.toml`/`.mise.toml`/`.tool-versions`, `.nvmrc`, `.python-version` | pinned toolchain |
| `Makefile`/`justfile`, `Dockerfile`, `docker-compose*.yml`, `.env.example` | how it's run, what it needs |

- **README:** prefer the root `README.md` (else `README*` at root, else the nearest one).
  Note a `docs/` index if there is one.
- **Agent/contributor rules:** `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING*`, `.cursorrules`.
  **They are the repo's own rules. Follow them.**
- **CI:** `.github/workflows/*.yml` (or the repo's CI config). Its commands are the ground
  truth for test, lint and build.

`read_file` the README and the primary manifest (with `offset` so you have real line numbers).

## 3. Render them for the operator
- **README → a `markdown` artifact**: `show_artifact(kind="markdown", title="<repo> — README",
  code=<README text>)`. Verbatim — **don't rewrite the author's words**. If it is very long,
  keep the sections up to usage/setup and end with `_[trimmed — full file: README.md]_`.
- **Primary manifest → `show_code`** on the meaningful range (for `package.json`: `scripts`
  through `dependencies`; for `pyproject.toml`: `[project]` deps and `[project.scripts]`),
  line numbers from `read_file(offset)` or `search_files`, with a one-line `note` ("scripts
  and runtime deps: `dev` runs Vite, the API is Express"). Check the lines it echoes back.
- **Monorepo:** `show_code` the root manifest (workspaces/turbo/pnpm config), and name each
  package with its one-line role in your reply. Don't open every package manifest.

## 4. Map and trace (for yourself: this feeds the overview, the card and the tour)
- `find_files` for the source tree, the tests, and the entry points (the manifest's
  `main`/`bin`/`scripts`, `**/main.*`, `**/index.*`, `**/server.*`, `**/app.*`, `cmd/**`).
  Read the entry point top to bottom once.
- Find the path that matters most (for an app: request or turn in → response out) and trace
  it with `search_files` (imports, function names) through 3–6 hops. Note each hop as
  `path:line — what happens`. Note the data stores and external services it touches (DB
  client, ORM schema, queues, third-party APIs, env vars).
- Spot **the 3 key flows** (e.g. "sign in", "create a gang", "render the dashboard") — you
  offer them at the end.
- For an AI/agent app, specifically locate: where the model is called (SDK client,
  `messages.create`, `chat.completions`, a raw HTTP call), the loop that handles tool calls
  and `stop_reason` / `finish_reason`, where the final answer is extracted, and the config/env
  it depends on (API keys, model names, timeouts, max tokens/iterations).

## 5. Architecture overview → a code-linked Mermaid `flowchart`
Follow the **`diagramming-code`** skill (read first, anchors, check the reply) — the rules
there apply in full. For the overview specifically:
- `show_artifact(kind="mermaid", title="<repo> — architecture overview", code="flowchart LR …", links=…)`.
- **5–12 nodes**: entry points → main modules/layers → data stores / external services.
  Group with `subgraph`s by layer (UI · API · domain · data) or by package in a monorepo.
  Short labels; edges say what flows (`-- "HTTP /api" -->`).
- **Only what you read.** Every node is grounded in a file you opened or a `search_files`
  hit, and **every node gets a link** with an `anchor` copied verbatim from that code (the
  definition, the route registration, the client construction). An external service with no
  code of its own links to where the code calls it (the client construction, the schema or
  migration). A node you can't ground doesn't go in.
- **Link keys are the bare node ids** (`PAGE`, `LIB`), never `participant:PAGE` — that prefix
  only works in sequence diagrams. Anchor the definition (`export default function
  FighterCard`), never an import line.
- Read the reply: fix links it **moved** to the wrong place, **dropped**, or says **match
  nothing**, with `update_artifact(old_string, new_string, links=…)` passing the **complete**
  map (a `links` argument replaces the stored one, it doesn't merge); `check_artifact` if it
  didn't report a render.
- Too big for 12 nodes → one overview of the top level, and offer drill-down diagrams per
  part at the end rather than drawing them now.

## 6. Prove the toolchain runs
With `run_command` (the operator may need to approve each one):
- **If there is a `mise.toml` / `.mise.toml` / `.tool-versions`, the toolchain is
  mise-managed. Don't rely on whatever runtime is on PATH.** Run `mise trust && mise install`
  first (mise refuses an untrusted config in a fresh clone), check with
  `mise exec -- <runtime> --version`, and from then on prefix every toolchain command with
  `mise exec --`. Report the versions mise resolved in the repo card.
- Install dependencies with the repo's own package manager (`npm ci` when a lockfile exists,
  `pnpm install --frozen-lockfile`, `uv sync`, `go mod download`, …).
- Run the test command from CI or the manifest, and the typecheck/lint if there is one
  (`npx tsc --noEmit` for TypeScript).

Record what passed, what failed, and how long it took. **A failing baseline is a finding,
not something to fix yet.** Report it and keep going. No test suite → say so.

## 7. Repo card, then hand over
Reply with a card (≤ 25 lines) and save it with
`memory_ingest(content=<card>, domain="repo:<name>", heading="<name> repo card")`:

```
<name> — <one-line purpose>
Stack: <lang/runtime/versions> · pkg mgr: <x> · toolchain: <mise/nvm/uv/...>
Run: <cmd>   Test: <cmd>   Typecheck/Lint: <cmd>   Build: <cmd>
Entry: <path:line>
Layout: <monorepo packages, or the top-level dirs that matter>
Core flow: <a → b → c, with path:line>
Data/external: <DB, services, APIs>
Config/env: <vars that matter>
Conventions: <from AGENTS.md/CONTRIBUTING/lint config>
Baseline: tests <pass/fail N>, typecheck <ok/errors>
Watch out: <anything surprising>
```

Point at the three artifacts in a line ("README, the manifest and the overview are open
beside chat — click any node to jump to its code"), then **end the turn with this choice**,
all three options, in one line: *"Want a guided tour of <flow 1>, <flow 2> or <flow 3>, a
sequence diagram of one of them, or straight to the bug?"*

## 8. Guided tour (only if the operator wants it; ONE stop per turn)
Walk the chosen flow as 3–5 stops (e.g. entry → request/turn loop → model call → tool
execution → answer extraction). At each stop:
- `show_code` on the key range with a short `note` saying what this stop does, so the
  operator reads the real code in the pane beside chat (line numbers from `search_files` or
  `read_file(offset)`),
- explain in 2–4 sentences what this piece does and the contract it relies on (e.g. what the
  `stop_reason` values mean, what shape the response has),
- ask one short question that checks or builds understanding (*"What do you think happens
  here if the model asks for two tools at once?"*), or invite theirs (*"Anything here you
  want to dig into?"*).

A sequence diagram of a flow follows `diagramming-code` (a `sequenceDiagram`, `msg:<n>`
links). Move to the next stop only when the operator says so. Skip ahead if they say
"got it" or "let's debug".
