# ADR 0015 — Optional native Discord surface (ingress + outbound)

> **Update (2026-06-18):** the Discord surface has been **extracted from core** into a
> standalone external plugin ([`protoLabsAI/discord-plugin`](https://github.com/protoLabsAI/discord-plugin),
> ADR 0058) — installed at runtime, not bundled. The gateway/conversation/context/
> turn_log/return_address modules (formerly `surfaces/discord/`) and the outbound tools
> (formerly `tools/discord_tools.py`) now live in that repo. The design below is
> unchanged — only its location.

- **Status:** Accepted (2026-06-03); extracted to an external plugin (2026-06-18)
- **Date:** 2026-06-03
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** surface, ingress, discord, reactive, operator, opt-in, deployment
- **Related:** builds on [ADR 0003](./0003-reactive-agent-activity-thread.md) (inbox/event bus); follows the opt-in posture of [ADR 0010](./0010-headless-setup-and-ui-tiers.md); contrasts with [ADR 0001](./0001-extensibility-and-plugin-architecture.md) (MCP/plugins). Source patterns: `protoLabsAI/-deprecated-gina`.

> Accepted. A personal-assistant / operator agent has to reach the operator
> **where they already are** — and receive from there too. Discord is the proven
> 1:1 + channel surface. `-deprecated-gina` shipped a strong, self-contained
> native Discord stack; rather than re-deriving it per fork, the template gets it
> as an **optional native surface** (off by default), routed through the existing
> reactive inbox so the whole fleet can turn Discord on with a token.

## 1. Context & Problem Statement

The template can already be *driven* (console, A2A, scheduler) and can *react*
to inbound stimuli via the **inbox/event bus** ([ADR 0003](./0003-reactive-agent-activity-thread.md)).
What it lacks is a **chat-platform ingress** — a way for the operator to talk to
the agent from a tool they live in, and for the agent to deliver back
(briefings, "remind me…" results, proactive nudges).

`-deprecated-gina` solved this with ~780 LOC of self-contained Discord code that
is genuinely good and battle-tested:

- **No `discord.py` dependency** — raw Discord **Gateway + REST v10** over
  `httpx` + `websockets`.
- **Inbound gateway** (`discord_bot.py`) — DMs + @-mentions, with: **burst
  debounce** (coalesce a rapid run of messages into one LLM call), **conversation
  continuity** (a conversation id flows as the OpenAI `user` field → LangGraph
  keys its thread by it), **slow-response reactions** (👀→✅ *only* when a turn is
  slow, never spammy), **auto-threading** of long guild replies, an **admin
  allowlist**, and **identity capture** — recording the DM `channel_id` as a
  **return address** so scheduler-fired / proactive turns have somewhere to land.
- **Outbound REST tools** — `discord_send` / `discord_read` / `discord_react`.

Two forces make this a template decision, not a per-fork copy:

1. **It doesn't fit MCP.** MCP ([ADR 0001](./0001-extensibility-and-plugin-architecture.md))
   is request/response. The inbound gateway is a **persistent, stateful
   connection** (debounce buffers, conversation windows, reaction/thread state).
   That's a native runtime surface, not a tool server. (The *outbound* half is
   tool-shaped and could be MCP — see §2.2.)
2. **Every operator/assistant fork wants it.** gina needs it now; roxy, jon, and
   future assistant forks will too. Re-porting 780 LOC per fork is waste.

## 2. Decision

Ship Discord as an **optional native surface in the template**, off unless
configured — the same opt-in posture as the `--ui` tiers (ADR 0010) and the
scheduler. Port the proven UX **in full** (the patterns above are cheaper to
carry forward than to re-derive). Two layers:

### 2.1 Inbound gateway → invokes the agent as a chat surface

A native background task (uvicorn lifespan hook, shares the event loop), **off
unless `DISCORD_BOT_TOKEN` is set**. It owns the persistent Gateway v10
connection and the stateful UX (debounce, continuity, reactions, threads,
allowlist).

**Refined during implementation (#490-series):** the original framing here was
"route Discord inbound *through* the ADR-0003 inbox as a stimulus." On building
it, that turned out wrong for a 1:1 DM, which is **conversational** — the inbox
fires into a *single* `system:activity` thread, which would collapse every
Discord conversation into one thread and destroy the per-DM continuity that
makes Discord useful. So the gateway instead invokes the agent as a **chat
surface**: it calls the in-process `chat(prompt, session_id)` entry with a
**per-conversation `session_id`** (the LangGraph thread key, surface-tagged
`discord-dm:…` / `discord-channel-…` for provenance), so each conversation keeps
its own thread. It still **publishes a `discord.message` bus event** so the
console can surface Discord activity (the ADR-0003 visibility touchpoint). The
inbox stays the right substrate for *non-conversational* pushes (webhooks, cron);
a live DM is a chat turn, not a one-shot stimulus. The agent invoker is injected
(`start_in_background(invoke, publish=…)`), keeping the surface decoupled and
unit-testable.

### 2.2 Outbound tools (stateless REST v10)

`discord_send` / `discord_read` / `discord_react` ship as **native starter
tools** (`tools/`), gated on the same `DISCORD_BOT_TOKEN`, registered in
`get_all_tools()` and droppable like any other. (They're stateless and could be
an MCP server instead; we keep them native for v1 — one token, one deploy, no
second process. Revisit if a non-Python consumer needs them — see §7.)

### 2.3 Opt-in & packaging

- **Off by default.** No token → the gateway never starts and the tools refuse
  with a clear "set `DISCORD_BOT_TOKEN`" message. Zero behavior change for forks
  that don't use it.
- **Config:** a `discord` block (timeouts, debounce window, slow-reaction
  threshold, admin IDs) + `DISCORD_BOT_TOKEN` in `config/secrets.yaml`
  (gitignored) / env. Sensible defaults so the only required input is the token.
- **Dependencies:** `httpx` is already core; `websockets` becomes an **optional
  dep** (like the `google` extra) — the gateway import-guards on it.

### 2.4 Return-address delivery (closes the proactive loop)

Identity capture stores the operator's DM `channel_id`. Scheduler-fired and
proactive turns (which have no originating caller) resolve their delivery target
from it — this is what makes "remind me in 30 minutes" actually *arrive*. The
return address is one more field on the operator-identity the agent already
keeps; delivery reuses the outbound `discord_send`.

## 3. Mechanism summary (for the build)

- `surfaces/discord/gateway.py` — the inbound listener (ported `discord_bot.py`),
  publishing to the event bus instead of calling chat directly.
- `surfaces/discord/rest.py` + `tools/discord_tools.py` — outbound REST + the
  three tools.
- Lifespan hook in `server.py`, guarded on `DISCORD_BOT_TOKEN` + `websockets`.
- `graph/config.py` — a `discord` config section; `config/langgraph-config.example.yaml` documents it.
- Event-bus stimulus type for Discord inbound; inbox/Activity surfaces it.
- Tests ported: gateway accept/debounce/continuity, context assembly, reaction
  lifecycle, return-address capture.
- Docs: `docs/guides/` Discord setup (bot creation, Message Content Intent,
  intents, one-bot-per-agent) + wire into the VitePress sidebar.

## 4. Security / safety

- **Token** in `config/secrets.yaml` (gitignored) / env — never committed.
- **Inbound auth** rides the inbox's auth model, not a new open endpoint; plus an
  **admin allowlist** (`discord.admin_ids`) — when set, only listed Discord user
  IDs are answered; when unset, the operator chooses (default-closed is
  recommended for a personal assistant).
- **Message Content Intent** is privileged — documented as a required Developer
  Portal toggle.
- **One gateway connection per token** — Discord evicts a second listener on the
  same token, so it's **one bot per agent** (multi-instance note, cf. ADR 0004).
- Respect REST rate limits (429 + retry-after) in the outbound layer.

## 5. Consequences

- **Fleet-wide Discord** as a turn-key opt-in; no fork re-implements it.
- **gina's v1 Discord slice changes**: instead of "Discord via MCP," gina
  **enables the template surface + configures** (token, admin IDs, channels) —
  far less work, and the inbound half is no longer mis-scoped onto MCP. The
  [gina v1 scope](https://github.com/protoLabsAI/gina) is updated accordingly.
- Adds an optional `websockets` dep and a new background task surface to own
  (reconnect/backoff, gateway version drift).
- Reinforces ADR 0003: the inbox is now the single ingress substrate for
  *all* inbound stimuli (HTTP, scheduler, **Discord**).

## 6. Alternatives considered

- **Discord as an MCP server (inbound + outbound).** Rejected for inbound: MCP
  can't host a persistent, stateful gateway connection. Viable for outbound only,
  which doesn't justify a second process for v1.
- **Depend on `discord.py`.** Rejected: heavy, opinionated event loop; the raw
  Gateway/REST v10 approach is already written, dependency-light, and proven.
- **Gina-only native port.** Rejected: the fleet would miss out and we'd
  re-extract to the template later anyway.
- **Generic "chat bridge" abstraction (Discord + Slack + …) now.** Deferred:
  build the Discord surface concretely first; factor a shared adapter shape only
  once a second platform actually lands (avoid speculative generality).

## 7. Open questions

- **Outbound as native tools vs MCP** — native for v1; revisit if a non-Python
  consumer needs Discord send/read.
- **Reconnect/resume** — gateway `RESUME` vs reconnect-fresh on disconnect;
  backoff policy.
- **Slash commands / interactions** — out of scope for v1 (DMs + mentions only);
  a later add.
- **Multi-guild / channel routing** beyond the operator's primary DM.

## 8. Related

- [ADR 0003](./0003-reactive-agent-activity-thread.md) — the inbox/event-bus
  substrate Discord inbound plugs into.
- [ADR 0010](./0010-headless-setup-and-ui-tiers.md) — the opt-in / optional-dep
  posture this mirrors.
- [ADR 0001](./0001-extensibility-and-plugin-architecture.md) — MCP/plugins, and
  why the inbound gateway is *not* one.
- `protoLabsAI/-deprecated-gina` — `discord_bot.py`, `tools/discord_tools.py`,
  `context_assembler.py`, `discord_log.py`, `docs/discord.md` (the ported source).
