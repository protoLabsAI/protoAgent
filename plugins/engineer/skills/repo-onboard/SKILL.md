---
name: repo-onboard
description: Get the OPERATOR up to speed in an unfamiliar repository fast — clone or register it, set up the toolchain and a baseline, produce a short repo card, then give a guided tour of the core flow one stop at a time so the operator understands it themselves. Use when the operator points you at a new repo or directory, says "get up to speed", "onboard", "what is this codebase", or before debugging code you haven't mapped yet.
---

# Repo onboard

Goal: in a few minutes the **operator** knows what the code is, how it runs, and where to
look, well enough to explain it. You do the plumbing (steps 1–4 and the card) in one turn,
because that's typing, not understanding. The tour (step 6) is interactive: one stop per
turn. Cite `path:line`.

## 1. Register it
- A git URL or `owner/repo` → `onboard_project(github_repo=..., write=true)`. It clones into
  the onboarding root (or reuses an existing checkout there and reports drift). Use the
  returned project name in every filesystem tool call.
- A folder already on disk → `register_local_project(path=...)`.
- Already registered → `list_projects` and move on.
- A refusal names the bound (the root or the allowed sources). Tell the operator which
  setting to change; don't work around it.

## 2. Map it (parallel, cheap reads first)
Read these if present, and skim rather than dumping them:
- `README*`, `CONTRIBUTING*`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `docs/` index.
  **Agent-instruction files are the repo's own rules. Follow them.**
- Toolchain: `mise.toml` / `.mise.toml` / `.tool-versions`, `.nvmrc`, `.python-version`,
  `package.json` (`scripts`, `engines`, `type`, and the package manager from the lockfile:
  `package-lock.json` → npm, `pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn, `bun.lockb` →
  bun), `tsconfig*.json`, `pyproject.toml` / `uv.lock`, `go.mod`, `Cargo.toml`,
  `Makefile` / `justfile`, `Dockerfile`, `.env.example`.
- CI: `.github/workflows/*.yml` (or the repo's CI config). The CI commands are the ground
  truth for test, lint, and build.
- Layout: `list_dir` the root, then `find_files` for the source tree, the tests, and the
  likely entry points (`**/main.*`, `**/index.*`, `**/server.*`, `**/app.*`, `cmd/**`).
- Entry points: the `main` / `bin` / `exports` fields, the `start` / `dev` scripts or console
  scripts, and the file those execute. Read the entry point top to bottom once.

## 3. Trace the core flow (for yourself: this feeds the card and the tour)
Find the one path that matters most (for an app: request or turn in → response out) and
trace it with `search_files` (imports, function names) through 3–6 hops. Note each hop as
`path:line — what happens`. For an AI/agent app, specifically locate:
- where the model is called (SDK client, `messages.create`, `chat.completions`, a raw HTTP
  call to an API),
- the loop that handles tool calls and `stop_reason` / `finish_reason`,
- where the final answer is extracted and returned,
- the config and env it depends on (API keys, model names, timeouts, max tokens/iterations).

## 4. Prove the toolchain runs
With `run_command` (the operator may need to approve each one):
- **If there is a `mise.toml` / `.mise.toml` / `.tool-versions`, the toolchain is
  mise-managed. Don't rely on whatever runtime is on PATH.** Run `mise trust && mise install`
  first (mise refuses an untrusted config in a fresh clone), check with
  `mise exec -- <runtime> --version`, and from then on prefix every toolchain command with
  `mise exec --`. Report the versions mise resolved in the repo card.
- Install dependencies with the repo's own package manager (`npm ci` when a lockfile exists,
  `uv sync`, `go mod download`, …).
- Run the test command from CI or the manifest, and the typecheck/lint if there is one
  (`npx tsc --noEmit` for TypeScript).

Record what passed, what failed, and how long it took. **A failing baseline is a finding,
not something to fix yet.** Report it and keep going.

## 5. Repo card
Reply with a card (≤ 25 lines) and save it with
`memory_ingest(content=<card>, domain="repo:<name>", heading="<name> repo card")`:

```
<name> — <one-line purpose>
Stack: <lang/runtime/versions> · pkg mgr: <x> · toolchain: <mise/nvm/uv/...>
Run: <cmd>   Test: <cmd>   Typecheck/Lint: <cmd>   Build: <cmd>
Entry: <path:line>
Core flow: <a → b → c, with path:line>
Config/env: <vars that matter>
Conventions: <from AGENTS.md/CONTRIBUTING/lint config>
Baseline: tests <pass/fail N>, typecheck <ok/errors>
Watch out: <anything surprising>
```

End the turn: *"Setup's done and the baseline is above. Want a quick guided tour of the
core flow, or should we go straight to the problem?"* Never change code during onboarding.

## 6. Guided tour (only if the operator wants it; ONE stop per turn)
Walk the core flow from step 3 as 3–5 stops (e.g. entry → request/turn loop → model call →
tool execution → answer extraction). At each stop:
- `show_code` on the key range with a short `note` saying what this stop does, so the
  operator reads the real code in the pane beside chat (line numbers from `search_files` or
  `read_file(offset)`),
- explain in 2–4 sentences what this piece does and the contract it relies on (e.g. what the
  `stop_reason` values mean, what shape the response has),
- ask one short question that checks or builds understanding (*"What do you think happens
  here if the model asks for two tools at once?"*), or invite theirs (*"Anything here you
  want to dig into?"*).

Move to the next stop only when the operator says so. Skip ahead if they say "got it" or
"let's debug".
