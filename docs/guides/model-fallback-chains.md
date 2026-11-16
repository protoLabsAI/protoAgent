# Model fallback chains

A model role can run on a **chain** of models instead of a single one: a primary,
then one or more fallbacks tried in order. When the primary is rate-limited, out of
quota, or down, the next model in the chain answers instead — so a provider outage on
one model no longer stalls the work that depends on it. Before this
([ADR 0133](../adr/0133-model-roles-with-fallback-chains.md)), only the main model had
fallbacks; every other role ran on one model with nothing to fall back to.

Fallbacks only fire on **retryable** failures: a rate-limit or quota error (HTTP 429,
`rate_limit_error`, `usage_limit_reached`), a 5xx, a timeout, or a connection error. A
**bad request is never retried on another model** — a 4xx (other than 429) or a content
refusal surfaces as-is, because trying a different model would just fail the same way.

## Which roles take a chain

Every core model role uses a chain. Harvest and Facts inherit theirs rather than
accepting an active override. Each appears in **Settings → Model → Model
roles**, grouped the way the console shows them:

| Group | Role | YAML key |
|---|---|---|
| Main and aux | **Main** — the reasoning model that drives the turn | `model.name` |
| Main and aux | **Auxiliary** — the cheap/fast model for lighter calls | `routing.aux_model` |
| Memory | **Compaction** — summaries of old history near the context limit | `compaction.model` |
| Memory | **Harvest** — the session-end conversation summary | Inherits `routing.aux_model`; no active override |
| Memory | **Facts** — fact extraction from a conversation | Inherits Harvest; no active override |
| Memory | **Memory select** — choosing which memories to surface | `memory.select_model` |
| Memory | **Chat gist** — the short carry-over summary of a chat ([ADR 0132](../adr/0132-chat-context-carryover.md)) | `memory.gist_model` |
| Checks | **Goal eval** — the `llm` goal verifier | `goal.eval_model` |
| Checks | **Drift judge** — the SOUL drift judge | `soul.drift.judge.model` |
| Media | **Image describe** — describing attached or ingested images | `knowledge.image_describe_model` |
| Subagents | **Each subagent tier** — the delegated-task models | `subagents.<name>.model` |

**Harvest and Facts currently always inherit**: Facts → Harvest → aux → main.
Although Settings exposes `memory.harvest_model` and `memory.facts_model`, these
are reserved overrides; setting them in Settings or YAML does not change the
runtime harvest or fact-extraction chain. To change the models those passes use,
set `routing.aux_model`. The chain editor controls described below apply to
active overrides; setting an own chain for Harvest or Facts has no runtime effect.

Subagents collapse into one counted line in Settings, built from the agent's live
subagent registry rather than a fixed list, so a newly added subagent appears on its own.

**Embeddings (`embed_model`) and transcription (`transcribe_model`) are deliberately
excluded** — they stay single models. An embedding fallback would change vector
dimensions mid-corpus and silently corrupt search, and transcription is a different API
that a chat-model chain can't stand in for.

## How inheritance works

A role you leave **blank** inherits its parent's **whole chain** — not just the parent's
primary. The inheritance order is:

- A blank specific role (compaction, memory select, goal eval, …) → the **aux** chain.
- A blank **aux** → the **main** chain.
- **Facts** → **harvest** → aux → main always; their reserved override fields
  do not alter this inheritance.

A chain you **set** is used **exactly as listed, with no silent fall-through** to the
main model. An aux chain of two cheap models never escalates to the expensive main model
on its own — if you want the main model as a last resort, add it to the chain. This is
the same rule the project board's coder ladders follow.

Two roles are exceptions that inherit the **main** chain rather than aux:

- **Image describe.** Aux models often can't read images, while the main model usually
  can, so a blank image-describe role inherits the main chain. If no model in the
  resolved chain can read images, image description reports itself **unavailable** (the
  role shows `unavailable` with "None of these models can read images. Set its own chain
  with a vision model.") instead of failing on every call.
- **Drift judge.** A blank SOUL drift judge runs on the main chain too, so a config that
  only sets an aux model keeps judging drift on the main model, as it did before chains.

## Setting a chain

### In Settings

Open **Settings → Model → Model roles**. Each role is one row:

- A role with its **own** chain shows one row per model — the first marked **Primary**,
  the rest tried in order. Reorder with the up/down controls, drop a model with the
  remove (✕) control, and append another with **+ Add fallback**.
- A **blank** role shows a quiet **Inherits &lt;parent&gt;** line (e.g. *Inherits aux*);
  open it to see the full chain it inherits. Choose **Set its own chain** to give the
  role an explicit chain (it pre-fills from what it was inheriting), or **Use
  &lt;parent&gt;'s chain** to clear a set chain back to inheriting.
- The **main** chain can't be emptied — it always keeps at least its primary.

A model you type that isn't on any of your connections is kept but flagged ("Not a model
on any of your connections. It will be skipped until it is."). Changes apply live, the
same way the other routing settings do.

### In YAML

Every configurable role field takes either a **string** (one model — today's shape, unchanged) or an
ordered **list** (primary first, then each fallback):

```yaml
model:
  name: [anthropic-oauth:claude-opus-5-5, openai-codex:gpt-6.1-sol]
routing:
  aux_model: [anthropic-oauth:claude-haiku-5-5, openai-codex:gpt-6-luna]
compaction:
  model: anthropic-oauth:claude-haiku-5-5   # a string is a chain of one
goal:
  eval_model: ""                            # blank inherits the aux chain
```

`model.name` can be a list, so the main model itself falls back. The legacy
`routing.fallback_models` still works and is appended after the entire `model.name`
chain (after its last entry when it is a list) — so
`model.name` plus `routing.fallback_models` is the one main chain. Existing single-string
configs load unchanged; a string is just a chain of one. Each `<id>:<model>` entry can
name its own connection, so a chain can cross providers (your Claude subscription, then
Codex, then the gateway).

## Seeing when a fallback fired

When a role answers from a fallback rather than its primary, three places record it:

- **Settings → Model → Model roles.** The role shows a **"fell back Nh ago"** marker (a
  warning dot; minutes for the first hour) that stays for 24 hours. Open it to see which
  model answered and why the earlier ones failed ("was out of quota", "was rate-limited",
  "timed out", "was down").
- **The provider status API.** `GET /api/config/model-roles` returns each role's resolved
  chain, whether it's set or inherited (and from which parent), and its last fallback
  (`last_fallback`: the time, the models that failed, the one that answered, and a reason
  redacted to exception type and status — never a prompt or provider payload). Image
  describe reports `status: "unavailable"`, `reason: "no-vision"` when no model in its
  chain can read images.
- **The log.** A fallback logs once per failure streak (not on every call), as
  `Model role <role> answered from <model> after <reason>`.

## Worked example: a fleet's chains

The fleet sets a main and an aux chain and leaves the specific roles blank, so each one
inherits:

```yaml
model:
  name:
    - anthropic-oauth:claude-opus-5-5   # primary
    - openai-codex:gpt-6.1-sol          # then Codex
    - protolabs:protolabs/cloud         # then the gateway
routing:
  aux_model:
    - anthropic-oauth:claude-haiku-5-5  # primary
    - openai-codex:gpt-6-luna           # then Codex
    - protolabs:protolabs/fast          # then the gateway
```

With this config and every specific role left blank:

- **Compaction, harvest, facts, memory select, chat gist, and goal eval** run on the aux
  chain — Haiku, then `gpt-6-luna`, then `protolabs/fast`. If Haiku is rate-limited, the
  compaction summary comes from `gpt-6-luna`, and Settings shows *Compaction — fell back*.
- **Image describe** runs on the main chain — Opus, then `gpt-6.1-sol`, then
  `protolabs/cloud` — because its blank value inherits main, not aux.
- Nothing silently escalates. The aux-family roles stay on the three aux models; to let
  them reach Opus you'd add it to the aux chain. Roles with active overrides can
  also be given their own chain; Harvest and Facts always follow aux.

This is the failure the chains were built for: when one cheap model hits its usage limit,
compaction, harvest, facts, goal checks, and gists keep running on the next model in the
chain instead of all failing at once.
