# File GitHub issues (`/issue`)

Type `/issue` in chat to file a GitHub issue straight from the console. It's a
**user-only control command** — like [`/goal`](/guides/goal-mode), it short-circuits
the turn and is handled by the server. It is deliberately **not** an agent tool, so
the agent can't open issues on its own (the read-only GitHub tools in the
`github` plugin stay agent-facing; *creating* an issue is a write you keep in your
own hands).

## Syntax

```
/issue <title> [--bug|--feature] [--repo owner/name] [--label a,b] [--dry-run]

<body — the first newline ends the title/flags line; everything after is the body>
```

- The **first line** carries the title plus flags; everything after the first newline
  is the issue body (markdown).
- `--bug` applies the `bug` label; `--feature` (alias `--feat`/`--enhancement`) applies
  `enhancement`. `--label a,b` adds more.
- `--dry-run` shows exactly what would be filed without calling GitHub — useful to
  check the body before committing.

### Example

```
/issue Touchpad scroll dead in the delegate modal --bug

## Problem
The scroll wheel does nothing inside the delegate setup modal.

## Steps to reproduce
1. Open delegate setup  2. hover the modal body  3. scroll

## Expected vs. actual
Expected the body to scroll; nothing moves.

## Acceptance
Wheel scrolls the modal body on macOS + Linux.
```

## It writes issues that pass the gate

The body is checked against the **same requirements the repo's issue gate enforces**
(`.github/workflows/issue-gate.yml`), so anything `/issue` files clears the gate:

- always — a substantive body **and** a *Problem / What's-wrong / Motivation* section;
- `--bug` — also *Steps to reproduce / Evidence / Expected-vs-actual*;
- `--feature` — also a *Proposed direction* or *Acceptance* section.

If a required section is missing, nothing is filed — the command replies with what's
missing and a ready-to-fill scaffold. Run `/issue <title> --bug` with no body to get the
scaffold up front.

## Which repo

The target repo is resolved, in order:

1. an explicit `--repo owner/name`;
2. **Settings ▸ GitHub ▸ Default repo for /issue** (`github.default_repo`);
3. the `GITHUB_DEFAULT_REPO` (or `GH_REPO`) environment variable.

If none is set, the command asks for `--repo` rather than guess — no silent misrouting.

## Auth

Issue creation runs through the `gh` CLI. Set `GITHUB_TOKEN`/`GH_TOKEN` (needs **write**
scope on the target repo) or sign in with `gh auth login` on the host. Without write
auth `gh` returns a readable error, which `/issue` surfaces back to you.
