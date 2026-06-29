# Explanation

Understanding-oriented. Read these when you want to know *why* the template is shaped the way it is — grouped by **domain** (same order as the other sections).

## Agent core & runtime

| Page | Question it answers |
|---|---|
| [Architecture](/explanation/architecture) | How do the A2A handler, LangGraph runtime, and LiteLLM gateway fit together? |
| [Model output](/explanation/output-protocol) | Native reasoning, and the thin guard that strips provider-leaked `<think>` from answers |
| [Mid-turn steering](/explanation/steering) | How can I redirect the agent mid-turn without stopping and losing its work? |
| [LiteLLM gateway](/explanation/litellm-gateway) | Why route every call through a gateway instead of the provider SDK? |

## Knowledge & memory

| Page | Question it answers |
|---|---|
| [Memory & knowledge store](/explanation/memory-and-knowledge) | How does the agent remember across sessions — and why extract, not dump? |

## A2A, fleet & delegates

| Page | Question it answers |
|---|---|
| [A2A protocol](/explanation/a2a-protocol) | What does A2A actually require, and where do naive implementations go wrong? |
| [Cost & trace propagation](/explanation/cost-and-trace) | Why do we emit cost-v1 and parse `a2a.trace`, and why that specific shape? |

## Operate & deploy

| Page | Question it answers |
|---|---|
| [Security & trust model](/explanation/security-and-trust) | What's the trust posture — auth, redaction, origin checks, sandboxing? |
| [Tuning & cost](/explanation/tuning-and-cost) | Which cost/perf levers (compaction, aux-model routing, execute_code, prefix caching, failover) exist, and when to flip them? |

## Architecture decisions

The [ADRs](/adr/) record every significant design decision in MADR format (numbered, never deleted — superseded instead).
