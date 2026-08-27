# 0097 — Native OAuth-subscription providers (run Claude / ChatGPT on your own plan)

Status: **Proposed**

## Context

protoAgent's native pipeline (`graph/agent.py` → `graph/llm.py`) drives every model call
through `ChatOpenAI` pointed at an OpenAI-compatible **LiteLLM gateway**. Model selection
is gateway config, not code (`model.provider` is just a label there), and the gateway
authenticates to Anthropic/OpenAI/vLLM with **API keys** it holds server-side — billed
pay-per-token.

Users increasingly want to run protoAgent on a **coding-agent subscription** they already
pay for — a Claude Pro/Max plan or a ChatGPT/Codex plan — instead of metered API keys.
Today the only way to do that is `agent_runtime: acp:<agent>` (ADR 0033), which drives the
turn through an external coding agent over ACP. That works but is the *delegated* path: a
separate out-of-process brain with its own tool loop, not protoAgent's native pipeline.

The subscription endpoints are **not** the standard chat-completions API, which is why a
gateway API key can't reach them and `ChatOpenAI` alone can't speak them:

- **Claude subscription** → Anthropic **Messages API** with **Bearer** auth (not
  `x-api-key`), the beta headers `claude-code-20250219,oauth-2025-04-20`, a
  `claude-code/<version>` user-agent, and a system prompt whose first block is the Claude
  Code identity line. Anthropic's Agent SDK (2026-06) explicitly licenses a third-party app
  authenticating with a user's Claude subscription.
- **ChatGPT/Codex subscription** → OpenAI **Responses API** at
  `https://chatgpt.com/backend-api/codex` with a Bearer OAuth token, a `ChatGPT-Account-Id`
  header, the `codex_cli_rs` originator, `store=false`, and encrypted-reasoning replay.

Hermes (NousResearch) is the reference implementation (~8.5k LOC of auth + a 1.3k-line Codex
Responses adapter + a 2.8k-line Anthropic adapter). We do **not** need to port that: the
graph is model-agnostic — `create_agent(model=…)` and every middleware/subagent/compaction
path treat the model as a `BaseChatModel` and rebuild it through the single `create_llm`
factory — and `langchain-openai ≥ 1.0` / `langchain-anthropic ≥ 1.0` already implement the
Responses and Messages wire protocols. The gap is **auth + the subscription-specific knobs**,
not the transport.

## Decision

Add two **native OAuth-subscription providers**, selected by `model.provider`, dispatched in
`create_llm` before the gateway path. No gateway, no ACP; the rest of the pipeline is
unchanged.

| Provider | Client | Auth | Credentials |
| --- | --- | --- | --- |
| `anthropic-oauth` | `ChatAnthropic` (Bearer subclass) | `auth_token` + OAuth betas + claude-code UA | env override → protoAgent's refreshable store (box by default) → live Claude Code file/keychain fallback |
| `openai-codex` | `ChatOpenAI` (Responses API) | Bearer + `ChatGPT-Account-Id` + `store=false` + `include=[reasoning.encrypted_content]` | console sign-in mints a box-shared credential by default; explicit CLI import rotates and hands the live credential to protoAgent |

The asymmetry mirrors Hermes and is deliberate: Anthropic's OAuth client is painful to mint
independently, so we can borrow Claude Code's live token; OpenAI's device-code flow is
runnable standalone. OAuth refresh tokens are **single-use**, so a missing Codex store is
never silently bootstrapped from the CLI. Explicit import rotates the token immediately,
handing the live credential to protoAgent and requiring a subsequent `codex login` for
the CLI. The store retains vendor-origin provenance, so disconnect deletes protoAgent's
copy without remotely revoking a credential that originated in another application.
On upgraded instances, an existing legacy instance-local store remains the resolver and
sign-in target until fleet creation promotes it to the box tier.

### Seam

- `graph/providers/oauth.py` — credential resolution + Codex refresh/store (no model deps).
- `graph/providers/anthropic_oauth.py` — `_OAuthChatAnthropic` swaps `api_key`→`auth_token`
  in the one `_client_params` assembly point; a test fails loudly if langchain-anthropic
  restructures it.
- `graph/providers/openai_codex.py` — configures `ChatOpenAI` Responses fields.
- `graph/providers/__init__.py` — `build_native_oauth_llm` dispatch; builders imported
  **lazily** so the default gateway path never imports langchain-anthropic or touches a file.
- `graph/middleware/claude_code_identity.py` — prepends the Claude Code identity line as the
  first system block, innermost + idempotent, only for `anthropic-oauth`.

## Consequences

- **Aux/subagent slots inherit the provider.** With `anthropic-oauth`, `model.name` and the
  aux/compaction/subagent model ids must be real Claude ids (a gateway alias raises a clear
  error). A mixed setup (native main + gateway aux) is a follow-up.
- **Identity leakage.** `anthropic-oauth` tells the model it is Claude Code before the SOUL
  persona. Accepted (Hermes does the same); it's the price of OAuth routing.
- **ToS.** The Claude path is licensed by Anthropic's Agent SDK. The ChatGPT/Codex path is a
  grayer area — those tokens are intended for the Codex CLI/IDE — so `openai-codex` is
  opt-in and off by default.

## Live validation (2026-08-08)

`openai-codex` was validated end-to-end against a real ChatGPT subscription on a dev
instance: single-turn chat, streaming, and a **multi-turn tool loop** (with `store=false`)
all work and render clean text. Five Codex-backend constraints surfaced live and are now
handled — worth recording because none are in langchain's generic Responses support:

1. **Model ids are per-account.** `gpt-5.3-codex` is rejected ("not supported when using
   Codex with a ChatGPT account"); the account's real list comes from
   `GET /backend-api/codex/models` (here: `gpt-5.5`, `gpt-5.4-mini`, the `*-terra`/`*-luna`
   code-mode models). Use a real slug for `model.name`.
2. **No system-role input items** ("System messages are not allowed") — the system prompt
   must ride the Responses `instructions` field. Handled by `CodexResponsesInputMiddleware`.
3. **`max_output_tokens` is rejected** (the backend owns truncation) — the builder omits
   `max_tokens`.
4. **`stream=true` is mandatory** — protoAgent always streams, so this is automatic.
5. **List content rendering.** langchain-openai's Responses mode always returns
   *content blocks* (not a string), which protoAgent's answer pipeline — built for the
   gateway's string content — stringified raw. Fixed by extracting `AIMessage(.text)` at the
   stream + final-answer sites (a no-op for string content).

Encrypted-reasoning replay across turns did **not** block the tool loop in practice, so the
feared adapter gap is smaller than expected; deep multi-turn reasoning continuity across many
tool calls is still worth watching.

## In-console sign-in (2026-08-09)

Signing in no longer requires a terminal — the setup wizard (and Settings) drive the OAuth
flow directly (`graph/providers/oauth_login.py`, `/api/config/oauth/{start,poll,complete}`):

- **openai-codex** — OpenAI's device-code flow: "Sign in" requests a user-code, opens
  `auth.openai.com/codex/device`, and the console polls until the user approves, then
  exchanges + stores tokens. Validated live (real device codes issued).
- **anthropic-oauth** — Claude Code's PKCE flow: "Sign in" opens
  `platform.claude.com/oauth/authorize`; the user approves, Anthropic displays a
  `code#state`, they paste it back, and we exchange at `platform.claude.com/v1/oauth/token`
  and store the tokens in protoAgent's resolved store (box tier by default; an existing
  legacy instance override remains until promotion), refreshed on use.

**ToS escalation (deliberate, operator's call):** the Claude flow authenticates with Claude
Code's *own* public OAuth client id (`9d1c250a-…`) — i.e. protoAgent performs the login *as*
Claude Code, a step beyond reading the CLI's existing credentials. Opt-in; the operator
accepted it for this build. The Codex device flow uses OpenAI's published Codex client, the
same mechanism the Codex CLI uses.

## Credential lifecycle (2026-08-10, #2440 / #2441)

The first cut had sign-in but no exit and no concurrency safety. Both are now closed:

- **Disconnect / cancel / revoke (#2440).** `disconnect(provider)` best-effort revokes
  protoAgent's own token (OpenAI `/oauth/revoke`), **always** deletes protoAgent's
  resolved store even if revocation fails, and writes a disconnect marker so the
  provider does not auto-resolve (no Codex-CLI recovery, no stored/CLI Claude token)
  until an in-console sign-in reconnects. The vendor CLI's own auth file is never touched.
  Wizard **Cancel** now aborts the server-side pending flow. New routes:
  `/api/config/oauth/{cancel,disconnect}`. Marker + stores keep the owner-only ACL via the
  `atomic_write` funnel.
- **Serialized refresh (#2441).** Codex read→refresh→write is serialized by a per-store
  `threading.Lock` with a double-checked re-read, so two in-process consumers can't both
  spend the single-use refresh token. The later fleet amendment adds a stable provider
  scope lock around warm resolution, refresh, transfer, sign-in/import, and disconnect.

## Fleet inheritance amendment (2026-08-27, #3196)

This amendment supersedes the instance-scoped and automatic-bootstrap ownership wording
in the original decision and lifecycle notes above.

protoAgent-owned OAuth stores now default to the box tier, so every instance and fleet
sister resolves one shared credential owner. Fleet creation
never copies an OAuth JSON file: duplicating Codex's single-use refresh token would create
two owners and eventually invalidate one of them.

For upgrades that still have a legacy instance-local protoAgent store, the default
`inherit_config` fleet-create path transfers that store into the one box location only
after all other workspace setup succeeds. The transfer holds both store locks, preserves
the exact credential document (including provenance), refuses a different existing box
login or an explicit disconnect marker, and rolls back multi-provider write failures.
The new member then inherits the same store in place. Vendor CLI credentials remain
untouched, and `inherit_config: false` performs no transfer.

Because the resolved store path itself can change during that transfer, path-keyed locks
alone are insufficient. Each provider also has a stable box-root scope lock acquired
*before* path resolution by transfer, disconnect, refresh/invalidation, and sign-in/import
writes. The store-path lock nests inside it. This makes a waiting disconnect re-resolve
the post-transfer box owner instead of deleting the stale instance path, and brings
Anthropic refresh under the same cross-process serialization as Codex.

## Encrypted-reasoning replay was HALF wired (2026-08-27)

The live-validation note above records that encrypted-reasoning replay "did not block the
tool loop in practice". That was true of the tool loop and wrong about the wire: replay was
not absent, it was **half present**, and the missing half is a 400 that bricks a thread.

    400 invalid_encrypted_content — The encrypted content for item rs_… could not be
    verified. Reason: Encrypted content could not be decrypted or parsed.

With `store=false` the backend keeps no reasoning state, so a replayed reasoning item must
carry its own `encrypted_content`. langchain-openai's **streaming** Responses path never
captures that blob — it reads the item at `response.output_item.added` (where the field is
still null), and the terminal `response.completed` event rebuilds the full message but keeps
only `parsed`/usage/`response_metadata` from it. The item's `rs_…` **id** does survive into
`additional_kwargs["reasoning"]`, and langchain replays it. protoAgent always streams (the
backend mandates it), so this is the only shape it ever produced: an item referenced by an id
the backend never stored, with nothing to verify. `include=["reasoning.encrypted_content"]`
was asking for a blob nothing read.

Worse than a failed turn: the item is checkpointed, so every later turn in the thread re-sent
it and failed identically — the thread was bricked, the same failure class
`ToolCallRepairMiddleware` exists to heal for a dangling `tool_call`.

Two fixes, both containment (capturing the blob is still open, below):

- **`graph/providers/codex_client.py`** — `CodexChatOpenAI` sanitizes the outbound Responses
  `input`: a reasoning item with no blob is **dropped** (restoring the stateless continuity
  this ADR believed it already had), and an item that *does* carry one keeps it but loses its
  `id` (`store=false` cannot resolve an item id — the blob is self-contained).
- **`graph/middleware/codex_reasoning_replay.py`** — the same 400 can still arrive from
  causes the sender cannot see: `encrypted_content` is sealed to the endpoint that minted it,
  and this repo lets each slot name its own connection, each chat tab override the model per
  turn, and a failed turn retry against the fallback chain — every one of which replays one
  thread's history to an endpoint that did not mint it (a rotated credential does the same).
  `CodexReasoningReplayRecoveryMiddleware` strips the replay state, retries once, and then
  rewrites the offending assistant messages in place by id so the bad item leaves the
  checkpoint. Registered on the lead **and** subagent stacks; a no-op unless that specific
  error fires.

Hermes's Codex adapter (`agent/codex_responses_adapter.py`) reached the same rules
independently — including the id strip and a session-wide replay kill switch — and its
`_issuer_kind` stamp is the model for the cross-issuer filter listed below.

## Open items

- **Encrypted-reasoning replay is contained, not delivered.** Cross-turn reasoning
  continuity on `openai-codex` is OFF: capturing the blob needs an `output_item.done`
  handler for reasoning items that langchain-openai does not have (worth an upstream
  issue). Once captured, replayed items should carry an issuer stamp (endpoint + account)
  and be filtered when the current endpoint differs — Hermes's `_classify_responses_issuer`.
- **Claude end-to-end still unproven on a real subscription** — the sign-in URL + PKCE +
  refresh are unit-tested and the flow runs, but no Pro/Max approval has been driven here yet
  (tool loop, streaming, `cache_control`).
- Follow-ups: surface sign-in in the Settings model panel too (wizard done); per-tab provider
  switching (relates to ADR 0082); mixed native-main / gateway-aux slots.
