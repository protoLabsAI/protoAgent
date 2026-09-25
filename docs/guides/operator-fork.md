# Build an operator fork (like "Roxy")

protoAgent ships the **primitives** for a multi-project operator agent — a
fenced filesystem toolset, a project registry, and the reactive substrate
(scheduler / inbox / Activity). The *operator* itself — a persona that monitors
several workspaces and keeps work flowing — is a **fork**, assembled from those
primitives + the standard specialization mechanisms ([ADR 0007](/adr/0007-directory-aware-operator-agent)).
No operator code lives in the template; a fork is persona + skill + config.

This guide builds "Roxy", a ProtoMaker portfolio manager that **monitors and
unblocks** (it does not write code).

## 1. Fork the template

Create a new repo from protoAgent (it's a GitHub template). Keep upstream so you
can pull future primitive updates.

## 2. Enable the fenced filesystem toolset (read-only for a monitor)

```yaml
# config/langgraph-config.yaml
filesystem:
  enabled: true
  allow_run: true          # Roxy needs git/gh/br reads — run_command, fenced cwd
  projects:
    - { name: orbis,    path: /work/ORBIS,    write: false }   # monitor, never edit
    - { name: pixelgen, path: /work/pixelgen, write: false }
```

A monitor sets every project `write: false` — `read_file`/`list_dir`/
`find_files`/`search_files` + `run_command` give it everything it needs to
*sense* state; `write_file`/`edit_file` are refused. (A coding fork would set
`write: true` and lean on git-as-seatbelt.)

**Running on your own desktop?** Name your editor and the agent can hand you a
location instead of pasting it — "open the router for me" jumps Zed to the file:

```yaml
filesystem:
  editor_command: zed        # or "code -g" / "cursor -g"
```

That binds `open_in_editor(project, path, line?)`. It resolves through the same
fence (nothing outside a managed project opens), works in `write: false` projects,
and returns immediately. It is for showing *you* a file — the agent still reads
with `read_file`. Leave it unset on a headless or remote deploy.

**Continuing the chat in Zed.** With the `protoagent-acp` shim registered as a Zed
agent server, a conversation can move from the console to Zed's agent panel. Zed can't
link straight into an agent thread, so the hand-off is claimed: `open_in_editor`
offers the calling chat for that project (its tool result says so), and a new agent
thread you start in Zed within **2 minutes**, in the project folder, a folder inside it,
or a parent folder up to three levels above it (never `/` or your home folder itself),
continues it. A chat has at most one pending offer: offering it again replaces the
earlier one. It keeps the same A2A context, and the history is
replayed into Zed. You can do the same by hand: right-click a chat tab and choose
**Continue in Zed**. That item appears only while Settings ▸ Chat's external editor is
Zed, and it's hidden for incognito chats. If the code pane is on and has a file open that was
opened from that chat, the offer is scoped to that project and Zed jumps to the file. Otherwise any folder can claim it,
so switch to Zed yourself. The shim checks `GET /api/chat/sessions/{id}` → `active`
before sending, so a Zed prompt waits while a console turn on the same chat is still
running. Turn the tool's offer off with `filesystem.editor_handoff: false`.

Independently of that, the opt-in **code pane** toolset (`filesystem.code_pane: true`, off by
default) adds `show_code(project, path, line, end_line?, note?)`: it points the **console's** code
pane at a line range with a one-sentence note on why it matters
([ADR 0112](../adr/0112-console-code-pane.md)).
It needs no editor and works for remote members too, because the console fetches the
file through the fenced `GET /api/fs/file`. Secret-like files (`.env`, keys,
credentials) and binaries are refused.

## 3. Write the operator persona (`config/SOUL.md`)

The persona makes it a monitor-and-unblock manager, not a coder. Sketch:

```markdown
# Roxy — ProtoMaker Portfolio Manager

You manage a portfolio of ProtoMaker workspaces. Your job is to **keep work
flowing**: watch each project's board + PRs, spot stalls and blockers, and
**unblock** — by nudging/creating features, escalating to a human, or
dispatching the ProtoMaker team. You **do not write code yourself**; you
monitor, coordinate, and escalate. Prefer the smallest action that unblocks;
escalate anything consequential.
```

## 4. Add a `project-operations` skill (`config/skills/project-operations/SKILL.md`)

This is where the ProtoMaker-specific knowledge lives — *how* to read a
workspace's state with the generic tools (so the template stays domain-neutral):

```markdown
---
name: project-operations
description: >-
  Use whenever asked to check status, sweep projects, or unblock work. How to
  read a ProtoMaker workspace and decide if work is flowing.
tools: [list_projects, read_file, find_files, search_files, run_command, check_inbox, schedule_task]
---

# Keeping a portfolio flowing

For each project: read the board (`read_file .beads/issues.jsonl` → counts by
status, blocked items), the feature pipeline (`find_files .automaker/**`), and
git (`run_command "git status"`, `run_command "gh pr list"`). A project is
**stalled** if: open features with no active work, PRs idle > N days, CI red, or
a dirty tree on main. Summarize per project (flowing / stalled / blocked), then
**unblock**: nudge the team, open an issue, or escalate to the operator.
```

## 5. Wire the drivers

- **Summon via A2A** — Roxy is reached over the A2A endpoint (`message/send` /
  `message/stream`); set an `auth.token` so inbound is authenticated.
- **An external dispatcher** — any A2A planner can dispatch to Roxy as an A2A agent
  (it already speaks the `cost-v1` extension Roxy emits).
- **Scheduled sweeps** — a periodic `schedule_task` (or an external cron hitting
  A2A) fires a "sweep all projects" turn; the skill + persona do the rest.
- **Escalation** — surfaces via the inbox / Activity thread (ADR 0003).
- **(Optional) richer board actions** — add the `protolabs_studio` MCP server to
  `mcp.servers` for `query_board` / `start_agent` / `get_sitrep` etc.

## 6. Deploy isolated + headless

Give Roxy its own `PROTOAGENT_INSTANCE` (ADR 0004) so its stores don't collide
with other agents on shared storage; run it on its own port / container. A
monitor has no operator at a browser, so run the **lean, UI-less tier**
([ADR 0010](/adr/0010-headless-setup-and-ui-tiers)): `--ui none` (or
`PROTOAGENT_UI=none`) serves only API + A2A + `/metrics` — no console,
core deps only. Drop the live config + supply the gateway key via env, and setup
auto-completes on boot (or run `python -m server --setup` once); `GET /healthz`
reports readiness. The default Docker image already builds this lean tier.

---

That's the whole operator: **primitives (template) + persona + skill + config
(fork)**. Nothing about ProtoMaker or "portfolio management" ships in protoAgent
core — it stays a neutral template, and Roxy is a thin specialization.
