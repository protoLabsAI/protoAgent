# Explanation

Understanding-oriented. Read these when you want to know *why* the template is shaped the way it is.

| Page | Question it answers |
|---|---|
| [Architecture](/explanation/architecture) | How do the A2A handler, LangGraph runtime, and LiteLLM gateway fit together? |
| [A2A protocol](/explanation/a2a-protocol) | What does A2A actually require, and where do naive implementations go wrong? |
| [Cost & trace propagation](/explanation/cost-and-trace) | Why do we emit cost-v1 and parse `a2a.trace`, and why that specific shape? |
| [Output protocol](/explanation/output-protocol) | Why `<scratch_pad>` / `<output>` instead of whatever the model emits? |
| [LiteLLM gateway](/explanation/litellm-gateway) | Why route every call through a gateway instead of the provider SDK? |
| [Tuning & cost](/explanation/tuning-and-cost) | Which cost/perf levers (compaction, aux-model routing, execute_code, prefix caching, failover) exist, and when to flip them? |
