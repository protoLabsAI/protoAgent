# 0082 — Per-chat ACP runtime: hot-swap a coding agent like a model

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Josh Mabry
- **Tags:** acp, agent-runtime, model-selection, chat, hot-swap, continuity
- **Related:** ADR 0033 (pluggable agent runtime — the axis this extends), ADR 0024
  (ACP coding agent / `code_with`), ADR 0075 (external interfaces — `model use`, per-tab
  model), ADR 0069 (memory delivery — where chat history lives), ADR 0048 (settings IA —
  the runtime/model selects), #1993 (slice 1, agent registration — shipped)

## 1. Context & problem statement

We want an operator to **pick an ACP coding agent from a chat tab's model dropdown and
have that chat run on it** — hot-swapping the runtime the way they already hot-swap a
gateway model, per chat, as needed.

ADR 0033 (D1) deliberately made **runtime a separate axis from the model**: `model` is a
gateway alias; `agent_runtime` (`native` | `acp:<agent>`) selects *how* a turn executes.
That split is correct, but it created an asymmetry the operator now feels:

| | `model` (gateway alias) | `agent_runtime` (`acp:<agent>`) |
|---|---|---|
| Nature | **Stateless** — history in the LangGraph checkpointer | **Stateful, out-of-process** — history in the coding agent's own ACP session |
| Selected | **Per turn** — `request_metadata.model` → `state["model"]` → `ModelOverrideMiddleware` | **Top of turn**, from the **global** `STATE.graph_config` |
| Swappable per chat? | **Yes, today** | **No** |
| Tools / skills / `/effort` / `/compact` / HITL | Native loop's, all apply | The agent's own loop; native machinery bypassed |

The native-vs-ACP fork happens at the **top of the turn** — `server/chat.py`
`is_acp_runtime(STATE.graph_config)` (streaming ~`:1494`, non-streaming ~`:1812`) — and
wins **before** the per-turn model is ever read. The per-tab `model` only reaches
`_run_native_turn` on the *native* branch. So today the two are decided at different
layers and never meet.

Two facts make the naive fix wrong, and one makes it genuinely hard.

**Trap A — `acp:<agent>` as a lead model is a different, broken path.** `create_llm`
(`graph/llm.py:197`) already maps an `acp:<agent>` *model name* to `make_acp_aux_model` —
a **text-only, tool-less** relay meant for auxiliary calls (compaction / goal-eval). If a
per-tab `model="acp:claude"` simply fell through to `ModelOverrideMiddleware`, it would
build that relay **as the lead model** inside the native loop: no tool-calling, no ACP
session, wrong semantics. Picking an ACP agent must route to the real `AcpRuntime`, not
the aux relay.

**The hard part — history is split and never synced.** Native history lives in the
LangGraph checkpointer (keyed by `thread_id`). ACP history lives **inside the coding
agent's own stateful session** (`_ACP_RUNTIMES[thread_id]`), which replays *nothing* per
turn — `AcpRuntime.run_turn` sends only a per-turn knowledge delta + the message (persona
rides `AGENTS.md` files; `runtime/context.py` assembles `stable_prefix` + `volatile_delta`,
never a transcript). So a **mid-chat swap** means the new brain sees none of the prior
turns — in *either* direction (native→acp, acp→native, acp:A→acp:B). This is exactly *why*
ADR 0033 made runtime a session-stable axis, and it's the crux of this ADR.

## 2. Decision drivers

- Deliver the operator ask: choose an ACP agent per chat and have that chat run on it.
- Don't fake conversational continuity we can't honestly provide.
- Reuse the existing per-tab model plumbing (it already carries an arbitrary string
  end-to-end) and the existing per-thread `AcpRuntime` registry.
- Keep the native path and the current global-`agent_runtime` behavior unchanged for
  anyone who doesn't opt into per-chat selection.
- Stay honest in telemetry (ACP turns are gateway-cost-0; already handled).

## 3. Considered options (the continuity story)

**Option A — runtime fixed at chat creation, immutable for the thread.** Pick native /
`acp:<agent>` when starting a *new* chat; can't change mid-thread. Sidesteps continuity
entirely; matches the stateful-session reality. But it isn't a swap *within* a chat, so it
doesn't meet the ask. *Rejected as the primary UX* (kept as a fallback framing).

**Option B — true per-turn swap with history bridging.** On swap, project the other
store's transcript into the new runtime (native transcript → ACP first-prompt seed; ACP
turns → written back into the checkpointer as messages). Highest fidelity, but real
engineering: two-way history projection, replay token cost, and it **breaks the ACP
prompt-cache discipline** ADR 0033 D5 depends on (the seeded turn mutates the prefix).
*Deferred* — an upgrade path, not v1.

**Option C — per-turn swap with an explicit context boundary. ★ chosen.** Allow the swap;
insert a **visible system note** at the boundary ("Switched to Claude Code — earlier
context isn't carried across runtimes") and start the new runtime's session fresh. Key the
ACP session by **`(thread, agent)`** so each agent keeps its *own* continuous session if you
swap back to it. Honest about the constraint instead of faking it, low-risk, and a clean
base to add Option B later if demand appears.

## 4. Decision

Adopt **Option C**, implemented as the following.

### D1 — Unify the top-of-turn fork with the per-turn model
Replace the bare `is_acp_runtime(STATE.graph_config)` forks (`server/chat.py` streaming +
non-streaming) with a per-turn resolver:

```
kind, agent = resolve_turn_runtime(request_metadata, STATE.graph_config)
#   request_metadata.model == "acp:<agent>"  → ("acp", agent)     [per-turn wins]
#   else                                      → resolve_runtime(global config)
```

An `acp:<agent>` per-tab selection routes the turn to `_acp_drive_turn` for **that agent**;
everything else is unchanged (global default still applies when no per-tab runtime is set).

### D2 — Key the ACP session by `(thread, agent)`
Today `_ACP_RUNTIMES[thread_id]` assumes one runtime per thread. Re-key to
`(thread_id, agent)` and let `_get_acp_runtime_locked` / `AcpRuntime.__init__` take an
explicit `agent` (instead of only `resolve_runtime(config)`), so swapping agents builds a
fresh session for the new agent while the old agent's session survives for a swap-back. LRU
+ idle-TTL eviction and the busy-refcount are unchanged (now per `(thread, agent)`).

### D3 — Guard the trap
Ensure an `acp:*` value can **never** reach the native lead-model path: `resolve_turn_runtime`
consumes it before `_run_native_turn`, and `ModelOverrideMiddleware` / `create_llm` reject
(or ignore) an `acp:*` lead-model request as a belt-and-suspenders. The `acp:<agent>` →
aux-relay mapping in `create_llm` stays valid **only** for the explicit aux slots
(`aux_model`, `compaction.model`, `goal.eval_model`), never for the chat lead.

### D4 — The continuity boundary (Option C)
When a turn's runtime differs from the thread's previous turn (native↔acp or acp:A↔acp:B),
emit a **system note** into the chat thread marking the boundary, and start the new
runtime's session fresh. No transcript replay (that's Option B). Reuse the existing runtime
status signal — the console already flags "protoAgent skills/commands don't apply in
coding-agent mode" (`operator_api/runtime.py`) — and make that a **per-tab** state.

### D5 — Registration (the "alias") — **shipped in #1993**
A user-registered `acp.agents.<id>` (custom agent or launch-spec override) now surfaces
everywhere the built-ins do: the `agent_runtime` select, the aux-model dropdowns,
`GET /api/acp-agents`, and `protoagent runtime list`. `acp_agent_catalog()` /
`acp_runtime_options()` merge a `config.acp_agents` mapping over the canonical catalog, and
the `agent_runtime` select became dynamically sourced (like `model.name`) so a custom
`acp:<id>` validates on save. This is the registration substrate the chat picker consumes.

### D6 — Chat-picker UX
`apps/web/src/chat/ComposerModelSelect.tsx` is the only substantial frontend change:
(a) stop replacing the whole menu with a static "coding agent" label when the *global*
runtime is ACP — the "coding agent" state becomes **per-tab**; (b) merge `acp:<agent>`
options into the picker (grouped/separated from gateway aliases). `chat-store.ts`
(`ChatSession.model` is already an arbitrary persisted string) and `api.ts` (already
forwards `metadata.model` verbatim) need **no change**. Optional: a `/model` slash command,
symmetric with `/effort`.

### Explicitly out of scope (v1)
- Cross-runtime **history bridging** (Option B).
- `/effort` / `/compact` / HITL / skill-directive **parity under ACP** (an ACP turn keeps
  bypassing the native middleware stack, as today).
- A **UI to author** a custom `acp.agents.<id>` (registration stays config/YAML for now).

## 5. Consequences

- The operator gets a truthful per-chat runtime swap: the picker offers gateway models
  *and* ACP agents; switching is honest about the context boundary rather than silently
  losing history.
- The native path, the global `agent_runtime` default, and non-selecting chats are
  unchanged — this is additive and opt-in per tab.
- Telemetry stays honest (ACP turns are gateway-cost-0, labelled `acp:<agent>` — already
  implemented in `_acp_drive_turn`).
- `(thread, agent)` keying multiplies the live-ACP-session ceiling; the existing LRU +
  idle-TTL eviction bounds it.
- A later Option B (history bridging) can layer on without changing D1–D3.

## 6. Build order (slices)

1. **S1 — registration (D5).** ✅ Shipped in #1993 (config-aware catalog + pickers).
2. **S2 — routing (D1–D3).** Per-turn `resolve_turn_runtime`, `(thread, agent)` keying,
   explicit `agent` on `AcpRuntime`, the trap guard. The load-bearing backend change.
3. **S3 — UX + boundary (D4, D6).** Chat picker lists ACP aliases; per-tab runtime
   indicator; the boundary system-note.

## 7. References

- ADR 0033 §D1 (runtime is a separate axis), §D5 (ACP caching discipline / stateful
  sessions), `runtime/acp_runtime.py`, `runtime/context.py`.
- Seams: `server/chat.py` (`_get_acp_runtime_locked`, `_acp_drive_turn`, the two
  `is_acp_runtime` forks, `_run_native_turn`), `graph/llm.py:197` (the `acp:` aux-model
  mapping / Trap A), `graph/middleware/model_override.py`, `a2a_impl/executor.py`
  (`_request_metadata`).
