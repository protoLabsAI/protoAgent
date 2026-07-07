# Run on Hermes (preset)

Already running [Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch)?
One command wraps protoAgent around it:

```bash
protoagent hermes
protoagent up          # console: http://127.0.0.1:7870
```

Your Hermes — its identity, memory, skills, and model endpoint, everything in `~/.hermes`
— stays the **brain**, driving every turn over ACP. protoAgent becomes the **shell** around
it: the operator console, an A2A endpoint other agents can call, the scheduler, goals with
verifiers, knowledge, and fleet membership. Hermes's own self-improvement loop (memory,
skill distillation, curator) keeps running untouched, because its state never moves.

This is the Hermes-flavored case of the [ACP runtime](/guides/acp-runtime) (ADR 0033) —
Hermes ships an in-tree ACP adapter (`hermes-acp`), so it slots into the same seam as the
coding agents, but as a *general* personal agent rather than a coding specialist.

## What the preset does

`protoagent hermes` (= `protoagent runtime use hermes`) is idempotent and never clobbers
an explicit config on either side:

1. **Installs `hermes-acp` if missing** — `uv tool install 'hermes-agent[acp]' --with
   mcp==1.26.0`. The `--with` pin is load-bearing: the `[acp]` extra does **not** include
   the `mcp` SDK, and without it Hermes *silently* skips MCP registration — protoAgent's
   operator tools would never appear. A pre-existing install is checked and repaired.
2. **Makes the model configs agree — Hermes wins.** An OpenAI-compatible endpoint found in
   `~/.hermes/config.yaml` is imported as this instance's model config (protoAgent's
   auxiliary calls — compaction, goal evaluation, fact extraction — need a model too).
   Only when Hermes has no model and this instance does is the seeding reversed.
3. **Adopts `~/.hermes/SOUL.md` as the persona** — only while this instance's SOUL is
   still the shipped placeholder, and via soul history, so it's reviewable and revertible.
4. **Sets `agent_runtime: acp:hermes`.**

Skip everything except the runtime flip with `--no-bootstrap`.

## What Hermes gets

The session mounts protoAgent's **operator MCP server**, so Hermes sees the full tool
registry — tasks, `memory_*`, `notes_*`, `set_goal`, `schedule_task`, subagents, plugin
tools — next to its own built-ins. The persona file protoAgent writes into the session
cwd tells it to prefer the operator tools for anything that must persist. Restrict the
set with `operator_mcp.tools` ([ACP runtime guide](/guides/acp-runtime#enable-it)).

Inbound A2A messages and scheduled jobs run on the Hermes brain too — protoAgent routes
every streamed turn through the configured runtime.

## Notes & limits

- **State scoping**: Hermes state stays in `~/.hermes` (or `HERMES_HOME`, which the CLI
  honors). Multiple protoAgent instances therefore share one Hermes memory — point
  `HERMES_HOME` elsewhere per instance if you want isolation.
- **First turn is slow**: registering the operator MCP server into a fresh Hermes session
  takes a minute-plus; later turns are normal.
- **OAuth providers don't import**: if Hermes talks to its model via an OAuth login (Nous
  Portal etc.) there's no key to lift — configure this instance's model yourself
  (`protoagent model use ...`), or rely on the ACP-backed aux fallback.
- Like every non-native runtime: usage/cost telemetry is not metered (your Hermes
  provider bills you), and the goal *continuation* loop is native-only today.

## See also

- [Run on a coding agent (ACP runtime)](/guides/acp-runtime) — the general mechanism,
  config keys, and tool restriction.
- [ADR 0033](/adr/0033-pluggable-agent-runtime-acp) — the runtime seam design.
