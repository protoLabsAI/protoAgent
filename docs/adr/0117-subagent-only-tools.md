# 0117 — Subagent-only tools: a domain's tools live in the subagent that owns it

- Status: Proposed
- Date: 2026-10-07
- Builds on: [ADR 0002](./0002-reusable-subagent-workflows.md) (subagents and their tool allowlists), [ADR 0018](./0018-plugin-surfaces-routes-subagents.md) (plugin-contributed subagents), [ADR 0005](./0005-tool-pollution-and-progressive-disclosure.md) (tool pollution and `search_tools`).

## Context

A subagent's allowlist (`SubagentConfig.tools`) names tools from the **lead's** pool:
`_build_task_tools` snapshots the assembled toolset as its tool map, and `_subagent_tools`
resolves the allowlist against it. A tool a subagent needs is therefore always bound to the
lead too.

So delegation contains the *conversation* but not the *capability*. Frank (the homelab ops
agent) has two media subagents, yet his lead still carries every media tool and its schema on
every turn, and can call a write such as `media_grab` directly. `tools.disabled` can't help:
it runs before the snapshot, so disabling a tool for the lead disables it for every subagent.

## Decision

**D1.** A new config list, `tools.subagent_only` (`tools_subagent_only`). A named tool is no
longer the lead's: it is not in the graph's `bound_tools`, the lead model is never offered its
schema, and a lead call to it is blocked. Any subagent that allowlists it still gets it, and
the lead reaches it only through `task`.

**D2. Held tools stay executable; a middleware keeps the lead off them.** There are two
subagent paths:
- An in-graph `task` builds its own graph from the tool map `_build_task_tools` snapshots
  before the split, so it resolves held tools directly.
- A **background** subagent (`task(run_in_background=True)`, auto-background) runs the
  **lead graph** under a `subagent_fence` stamped from its allowlist (#1639).

The held tools therefore stay in the lead graph's ToolNode. `SubagentOnlyMiddleware` drops
their schemas from every unfenced (lead) model call and blocks an unfenced call to one with a
ToolMessage that says to delegate. A fenced pass is left to `SubagentFenceMiddleware`, so a
held tool runs exactly when the fence names it.

**D3. No side door.** A proxying late tool (execute_code's bridge) is built over the lead's
view only. The deferred `search_tools` meta-tool lists a held tool only to a call running
under a turn fence that names it, so in deferred mode a background subagent can still load
the schema while the lead never sees it. The split runs over the final toolset, so a held
late tool or `search_tools` itself is covered. The operator MCP never
serves a held tool, even by name: over that bus the caller is a lead (a foreign client, or
the ACP brain).

**D4. Not a deny list, and loud when misused.** `tools.disabled` still wins and removes a
tool from subagents as well. Graph build logs a held name that no registered subagent
allowlists, and one assembled after the `task` snapshot (filesystem and late tools), which
only a background subagent can reach.

**D5. A subagent's allowlist is a permission.** Agent self-config (`set_config`) refuses
`subagents.<name>.tools`, so the lead can't widen a subagent's allowlist to pull a held tool
back within reach. A subagent's model and `max_turns` stay writable. `tools` was already a
denied section, so the list itself can't be edited from inside.

**D6. Visible, not wired.** The compiled graph stamps `subagent_only_tools`. `/api/tools`
lists them with `subagent_only: true`, outside `count` (still "what the lead model can call"),
and the console's Tools list marks them "Subagents only". Checks that ask whether an action
is backed by a tool count held tools as backed: the persona untooled-action audit and the
archetype capability contract. So does `load_skill`'s availability note.

**D7. Out-of-graph subagent runs are unchanged.** `run_manual_subagent` (slash commands,
workflow steps, scheduled `/dream`) builds its own tool map from the full set, so a workflow
step whose subagent allowlists a held tool still gets it.

## Consequences

- An agent can hand a whole domain to one subagent: its tools, their schemas and the context
  they generate stay there. The lead's prompt shrinks by those schemas, and only the
  subagent's final answer comes back.
- Subagents can't call `ask_human` (no checkpointer to resume an interrupt), so a domain whose
  writes need operator approval runs in two passes: the subagent returns a plan, the lead
  asks, then re-dispatches with the approval stated. The brake stays in prompts, as it is for
  the lead today.
- `tools` is already a denied section for agent self-config (`tools.self_config_enabled`), so
  an agent cannot move its own tools in or out of this list.
- **"Fenced" means any fenced turn, not only a background subagent.** The middleware treats a
  turn that carries a `subagent_fence` naming a held tool as allowed to use it. Peer-channel
  turns (#2972) carry fences too, and an authenticated A2A or chat caller can supply one in
  request metadata. That caller already holds the operator bearer and could already drive the
  lead to any bound tool, so this adds no new capability. It does mean a held tool is a
  context boundary inside the agent, not a security boundary against its operator.
- **ACP runtime.** An agent whose main runtime is an ACP coding agent gets its tools over the
  operator MCP, which never serves held tools (D3). A background subagent on such an agent
  therefore can't use them; only in-graph `task` subagents can.

