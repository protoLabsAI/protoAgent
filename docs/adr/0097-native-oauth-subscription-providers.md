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
| `anthropic-oauth` | `ChatAnthropic` (Bearer subclass) | `auth_token` + OAuth betas + claude-code UA | **read live** from `~/.claude/.credentials.json` / `CLAUDE_CODE_OAUTH_TOKEN` (Claude Code owns login + refresh) |
| `openai-codex` | `ChatOpenAI` (Responses API) | Bearer + `ChatGPT-Account-Id` + `store=false` + `include=[reasoning.encrypted_content]` | **bootstrap-then-own**: import `~/.codex/auth.json` once, then keep + refresh our own instance-scoped copy |

The asymmetry mirrors Hermes and is deliberate: Anthropic's OAuth client is painful to mint
independently, so we borrow Claude Code's live token; OpenAI's device-code flow is runnable
standalone, and OAuth refresh tokens are **single-use**, so owning our own refreshed copy
avoids racing the Codex CLI to a 401.

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

## Open items (the live gate)

Unit tests cover dispatch, auth headers, credential resolution/refresh, and the identity
middleware, but a real subscription round-trip can't run in CI. To verify live:

1. **Claude** end-to-end turn on a Pro/Max token — tool loop, streaming, `cache_control`.
2. **Codex** multi-turn **encrypted-reasoning replay** across the LangGraph checkpointer with
   `store=false` — the one place langchain's Responses converter may fall short of Hermes's
   hand-rolled adapter, and the item most likely to need follow-up.
3. Token **refresh** under a real expiry; confirm we never rotate the Codex CLI's file.

Follow-ups: a native `protoagent auth login` device-code flow (vs. import-then-own); per-tab
provider switching (relates to ADR 0082); mixed native-main / gateway-aux slots.
