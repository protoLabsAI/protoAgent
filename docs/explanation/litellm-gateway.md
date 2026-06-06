# LiteLLM gateway

Every LLM call from the template routes through an OpenAI-compatible endpoint (`api_base: http://gateway:4000/v1`). The template assumes there's a LiteLLM gateway somewhere on the network. Why?

## The problem this solves

Fleet of agents, each wants to specify a model. Options:

1. Each agent hardcodes `model="claude-opus-4-6"` and imports `langchain-anthropic`.
2. Each agent reads a model name from env, but still imports the provider SDK.
3. Each agent talks to a single OpenAI-compatible endpoint, and the endpoint routes.

Option 3 wins because:

- Model upgrades happen in one place (gateway config). No cascading PRs across every agent in the fleet.
- New provider support (Gemini, DeepSeek, local vLLM) doesn't require each agent to add an SDK.
- A/B testing a new model is a gateway-level config change with rollback.
- Per-agent cost / rate-limit policies are enforced at the gateway, not per-agent.
- The OpenAI-compatible surface is the lowest-common-denominator every agent framework understands.

## The alias pattern

The template points at `model.name: protolabs/reasoning`. Two things to know:

1. **`protolabs/<name>` is a gateway alias**, not a real model. The gateway config maps `protolabs/reasoning` → whichever real model (e.g. `claude-opus-4-6`, `gpt-4o`) you want.
2. **Each agent gets its own alias**. Roxy uses `protolabs/roxy`, a researcher agent might use `protolabs/researcher`. Same gateway, different underlying models, different rate limits, different cost tracking.

To swap a model for an agent:

```yaml
# In the gateway's config.yaml
model_list:
  - model_name: protolabs/reasoning
    litellm_params:
      model: anthropic/claude-opus-4-6   # ← was claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
```

Reload the gateway. No agent restart needed — the next request picks up the new mapping.

## Why OpenAI-compatible specifically

LangChain has first-class support for `ChatOpenAI` with a `base_url` override. Pointing it at LiteLLM "just works" — no custom provider adapter needed on the agent side.

LangChain also has native provider clients (`ChatAnthropic`, `ChatGoogleGenerativeAI`, etc.), but using those re-couples the agent to a specific provider, which is exactly what the gateway is there to avoid.

## What you trade off

**Provider-specific features get harder.** If Anthropic releases a new API feature that OpenAI's spec doesn't map to cleanly (prompt caching, computer use, extended thinking output), LiteLLM's translation layer may not expose it — or may expose it via a non-standard extension field that `ChatOpenAI` ignores.

For most agent work this doesn't matter. When it does, the escape hatch is to import the provider SDK directly for that one call, bypassing the gateway — losing the centralization for that call, but only for that call.

**You pay a hop.** LiteLLM → provider adds one network hop per request. In practice this is negligible (sub-10ms on a local docker network), but it's real. If you're building latency-critical real-time inference, you might route around the gateway.

## What about `usage_metadata`?

LiteLLM is well-behaved about normalizing Anthropic's `usage.input_tokens` and OpenAI's `usage.prompt_tokens` into a single shape. The template's `on_chat_model_end` cost capture works identically whether the gateway is routing to Anthropic, OpenAI, or something self-hosted.

The one gotcha: `stream_usage=True` (passed in `graph/llm.py`) is required to get usage on streaming responses. See [Cost & trace](/explanation/cost-and-trace) for why.

## What about cost tracking at the gateway?

LiteLLM exposes per-call cost in its callback hooks, but the template computes `costUsd` **in-process** instead (via `pricing.py`, accumulated across the turn's LLM calls) and emits it on the `cost-v1` DataPart — so cost tracking doesn't depend on a gateway callback. See [Cost & trace propagation](/explanation/cost-and-trace).

## Why not just use an OpenAI key directly?

Fine for a single agent. Breaks down when you have a fleet because:

- API keys proliferate. Every agent has its own, each rotated independently.
- Cost aggregation requires parsing N provider billing pages.
- Switching a single agent to a different model requires code + deploy, not config + reload.
- Rate limits hit individual agents in isolation; cross-agent orchestration of limited quota is impossible.

The gateway solves all of these centrally. For a fleet, it's worth the hop.

## Related

- [Configuration reference](/reference/configuration) — the `model.*` keys
- [Environment variables](/reference/environment-variables) — `OPENAI_API_KEY` points at the gateway
- [Roxy's config](https://github.com/protoLabsAI/roxy/blob/main/config/langgraph-config.example.yaml) — a real fork's `model.*` gateway-alias config
