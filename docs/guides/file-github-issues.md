# File GitHub issues (`/issue`)

File a GitHub issue straight from the console — two ways, one backend path:

- the **`/issue` chat command** (type it, or pick it from the slash menu), and
- the **🐛 bug button** in the utility bar (bottom-left, next to Settings), which
  opens a **form dialog**.

It's **user-only** — like [`/goal`](/guides/goal-mode) the command short-circuits the
turn and is handled by the server; it is deliberately **not** an agent tool, so the
agent can't open issues on its own (the read-only GitHub tools in the `github` plugin
stay agent-facing; *creating* an issue is a write you keep in your own hands).

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

## The form dialog

The 🐛 button (and picking `/issue` from the slash menu) opens a form: **Type**
(Bug/Enhancement), **Repo**, **Title**, and the type-specific section fields. It
assembles a body with the exact headings the gate checks, so a dialog-filed issue
always conforms; on success it drops a `✓ Filed … <url>` note in the current chat.

The **Repo** field is a quick-toggle dropdown of your configured repos (see below),
with a **Custom…** option that swaps in a free-text box (an inline **×** returns you
to the list) for a one-off repo.

## Which repo

Configure the repos under **Settings ▸ System ▸ GitHub**:

- **Repos for /issue** (`github.repos`) — the `owner/name` list shown in the dialog's
  dropdown. Pairs with the [portfolio manager](/guides/portfolio)'s many-repo setup.
- **Default repo for /issue** (`github.default_repo`) — the preselected one (and the
  command's default). Blank = the first repo in the list.

For a single issue the target is resolved, in order: an explicit `--repo owner/name`
(or the dialog's Repo field) › the default above › the first configured repo › the
`GITHUB_DEFAULT_REPO` / `GH_REPO` env var. If none is set the command asks for `--repo`
rather than guess — no silent misrouting.

## Auth

Issue creation runs through the `gh` CLI. Set `GITHUB_TOKEN`/`GH_TOKEN` (needs **write**
scope on the target repo) or sign in with `gh auth login` on the host. Without write
auth `gh` returns a readable error, which `/issue` surfaces back to you.
