# 0075 — External interfaces: a first-class `protoagent` CLI + tightened MCP / API / model onboarding

- Status: Accepted
- Date: 2026-07-05
- Implementation: D1 CLI (#1816), D3 MCP profiles + HITL-hang fix (#1817), D5 `protoagent model`
  (#1821), D4 goal-API dedupe (#1822) · real `/v1` usage (#1825), and **D2 the shared `ops/` layer**
  — `GET /api/mcp/exposed` + resolver extraction (#1824), the `ops/` package + `knowledge.ingest`
  (#1826), `plugins.install_and_activate` (#1827), `config` + `fleet` ops (#1828), the
  `GET /api/operations` catalog + `protoagent operations`/`config get·set` CLI (#1830), and
  `protoagent knowledge ingest` (#1831) — are shipped. Remaining: the `safe-operator` MCP profile
  (needs the admin ops exposed as MCP tools + the ADR 0071 consent gate) and the D5 HuggingFace
  local-app registration (external PR).
- Builds on: ADR 0033 (ACP runtime + operator MCP server), 0027 (plugin CLI + installer),
  0041 (workspace CLI + tiered stores), 0042 (fleet CLI + supervisor), 0047 (settings
  cascade + `config explain`), 0019 (managed / consumed MCP), 0066 (federation token +
  the `/api` operator ceiling), 0071 (plugin trust-and-consent).
- Related: protoCLI `proto` — the A2A **client** + ACP **server** (ADR 0024/0025); #1504.

## Context

protoAgent has three external interfaces — a **CLI**, an **operator MCP server**, and an
**HTTP API** (`/api` + OpenAI-compat `/v1` + A2A `/a2a`). Each grew on its own, and an audit
(2026-07-05) found they are **two disjoint shared spines, not one operation model**:

- **Agent-tool spine** — the native tool-loop and the operator MCP both re-project
  `tools/lg_tools.py::get_all_tools()`. One source of truth; the MCP is a genuine
  re-projection, not a reimplementation. ✅
- **Instance-admin spine** — the CLI and the REST API both call the `graph/**` cores
  (`installer.install`, `fleet.supervisor`, `config_explain`, …). Shared per pair. ✅
- **The two spines barely intersect.** Admin operations (plugin install, fleet up, config
  edit, MCP-server management) are **not** agent tools, so they can never appear on the MCP;
  most agent tools have no REST twin. Only `knowledge_ingest` spans both — and it
  *duplicates* its extract/transcribe/add glue in the tool and the route.

Three concrete symptoms:

1. **`python -m server …` is the only admin front door, and it's undiscoverable.** The
   `plugin` / `workspace` / `fleet` / `skills` / `config` subcommands are hidden
   `if sys.argv[1] == "plugin"` branches in `server/__init__.py::_main()` — they don't even
   appear in `--help`. There is no lifecycle verb (`up`/`down`/`status`), no `setup`, no
   installable command name. It reads as an internal module invocation, not a product.
2. **"Operate the instance over MCP" is only half-true.** `server/operator_mcp.py` exposes
   the *conversational* toolset (memory / notes / tasks / goals / skills / `delegate_to`) but
   **not** administration — you cannot install a plugin, drive the fleet, or edit config over
   MCP. (It also has a real bug: `ask_human` / `request_user_input` are HITL `interrupt`
   tools included in the `"*"` set; called over a foreign stdio MCP they hang the turn.)
3. **The REST surface has drift** — `goals` vs `goal`, and one memory store split across
   `/api/memory` + `/api/knowledge` + `/api/memory/injections` (three modules, two prefixes);
   plus REST-only orchestration glue (install-and-activate, ingest) hand-written on top of
   the shared cores, so a CLI install can't hot-reload a live server.

The one part that is **already clean**: authentication. `a2a_impl/auth.py::A2AAuthMiddleware`
is a single default-deny gate covering `/api`, `/v1`, and `/a2a` identically (Bearer /
X-API-Key / federation-token / SSE query-token; open mode when unset). This ADR **reuses it
unchanged.**

Separately, onboarding a **local LLM** (Ollama, HuggingFace TGI, LM Studio, vLLM) to power
protoAgent is a pure config move today — `graph/llm.py::create_llm` builds a plain
`ChatOpenAI(base_url=config.api_base, model=config.model_name)`, so the LiteLLM gateway is the
**default, not a lock-in** — but there is no discovery, no model command, and no "use in app"
handoff, so it isn't *fast*.

`proto` (protoCLI) already owns two legs of the agent relationship — a faithful **A2A client**
(`packages/cli/src/a2a-client/`, mirroring `evals/client.py` + the `:7870-7879` port-scan) and
an **ACP server** (`proto --acp`, so protoAgent's `delegate_to acp` spawns it). It has **zero**
runtime lifecycle — it assumes a protoAgent is already running. So the missing leg is
unambiguously protoAgent's own to own.

## Decision

Treat the CLI, MCP, and API as **one operation model with three faithful projections**, and
give protoAgent a first-class terminal identity. Five decisions:

- **D1 — A first-class `protoagent` CLI: the admin + lifecycle control plane.** Retire
  `python -m server <sub>` as the user-facing entrypoint in favor of an installable
  `protoagent` command with a real subcommand tree and `--help`. Keep `proto` as the
  interactive **A2A chat client**; the two meet at the wire (A2A / ACP), never in one binary.
  *Locked: the command is named `protoagent`.*
- **D2 — A shared `ops/` layer.** Promote the currently REST-only orchestration
  (install-and-activate, ingest, fleet up, config set) into one function per operation that
  carries the full orchestration, consumed by **all three** surfaces. "One operation, three
  projections" — the CLI verb, the MCP tool, and the REST endpoint call the *same* op.
- **D3 — Extend the operator MCP to the FULL admin surface, via curated profiles + consent.**
  The admin ops from D2 become MCP-callable, so a foreign brain (Claude Desktop, Cursor) can
  `plugin install`, `fleet up`, assign work, etc. Gated by (a) curated **profiles**
  (`read-only` / `safe-operator` / `full`) instead of all-or-name-by-name, (b) ADR 0071
  trust-and-consent for the admin tier, and (c) the ADR 0066 `/api`-ceiling posture (admin ops
  are the very set that ceiling protects). Fix the `ask_human`/`request_user_input` hang
  (exclude from `"*"`, like `execute_code`). *Locked: full admin over MCP.*
- **D4 — Tighten the HTTP API now, breaking changes accepted.** Dedupe the split namespaces
  (`goal[s]`, `memory`/`knowledge`/`injections`), fill in real `/v1` `usage` (stubbed to
  zeros today), and publish a machine-readable **operation catalog** the three surfaces derive
  from. Ship one release of deprecation aliases, then delete. *Locked: do it now, complete it.*
- **D5 — Fast local-LLM onboarding + "use in app".** `protoagent setup` discovers running
  local providers; a `protoagent model` verb group manages the model; and protoAgent registers
  as a **HuggingFace "Use this model" local-app** so a HF model card hands a model straight to
  it. *Revised after research (2026-07-05): for an agent runtime the "use in app" contract is a
  **CLI snippet via a PR to `huggingface.js` `local-apps.ts`**, NOT a `protoagent://` deep-link
  — protoAgent maps onto the existing `hermes-agent`/`openclaw`/`pi` pattern (which are the
  Hermes/OpenClaw competitors, already registered on the same cards). A deep-link is only for a
  GUI loader (optional, secondary). Locked: ship both `uv tool`/`pipx` and a zero-Python
  frozen-binary `curl | sh` install path.*

## Design details

### A. The `protoagent` CLI

A `console_scripts` entry (`protoagent = "server.cli:main"`) over a real `argparse`/`typer`
subparser tree. The existing dispatchers are **re-parented, not rewritten**:

```
protoagent setup                     # interactive provider/model/key wizard  (new; mirrors proto setup)
protoagent up | down | status        # lifecycle: start/stop/inspect the default instance  (new)
protoagent serve [--port]            # run in the foreground  (= today's `python -m server`)
protoagent plugin   …                # ← graph/plugins/cli.py        (exists — re-parent)
protoagent workspace …               # ← graph/workspaces/cli.py     (exists — re-parent)
protoagent fleet    …                # ← graph/fleet/cli.py          (exists — re-parent)
protoagent skills   …                # ← graph/skills/cli.py         (exists — re-parent)
protoagent config   …                # ← graph/config_explain.py     (exists — re-parent)
protoagent knowledge ingest|search   # over the store / the D2 ingest op  (new, thin)
protoagent model    …                # local-LLM onboarding (see E)  (new)
protoagent mcp      serve|profiles   # run/inspect the operator MCP (see C)  (new, thin)
```

`up`/`down`/`status` reuse the fleet supervisor's process model (detached spawn + boot-watch +
liveness) applied to the default instance — no new supervision primitive. Chat is deliberately
**absent**: it's `proto`'s job. `python -m server` keeps working as the internal module form
(the frozen sidecar re-invokes it); `protoagent serve` is the friendly alias.

### B. The shared `ops/` layer

A new `ops/` package: one module per domain, each a function that takes plain args + a context
and returns a plain result, wrapping the `graph/**` core **plus** the orchestration that is
REST-only today. e.g. `ops.plugins.install_and_activate(url, ref, *, activate: bool)` calls
`installer.install` **and** the auto-enable + hot-reload dance (`plugin_routes.py` L202-260)
that a CLI install currently can't do. The three surfaces become thin adapters:

- CLI adapter → parse argv → call the op → render text/JSON.
- REST route → validate body → call the op → JSON.
- MCP tool → the op wrapped by `to_fastmcp`, admitted by profile (C).

`knowledge_ingest` (the one cross-spine op) collapses to a single `ops.knowledge.ingest`
shared by the agent tool **and** the route, ending the double-glue.

### C. Operator MCP: admin profiles + consent

Extend `operator_mcp.operator_tools()` to admit the D2 admin ops, selected by a named
**profile** rather than a raw allowlist:

- `read-only` — status / list / explain / search (no mutation).
- `safe-operator` — read-only + curation + non-destructive ops; **no** install / fleet
  control / config-rewrite.
- `full` — everything, incl. `plugin install`, `fleet up/down`, `config set`, work
  assignment. Requires an explicit consent ack (ADR 0071) recorded per client, and honors the
  ADR 0066 `/api` ceiling (a federation-token peer never reaches `full`).

The `"*"` set excludes the HITL interrupt tools (`ask_human`, `request_user_input`) alongside
`execute_code`. Add `GET /api/mcp/exposed` so the exposed set is introspectable (today only
the *consumed* MCP is listed).

### D. HTTP API tightening (breaking, aliased one release)

- **Namespaces:** collapse `goal` → `goals` (member = `/api/goals/{session_id}`); unify the
  memory store under `/api/memory/*` with `knowledge` chunks and `injections` as sub-resources
  (`/api/memory/chunks`, `/api/memory/injections`). Keep the old paths as **307-aliases with a
  `Deprecation` header** for one minor release, then delete.
- **`/v1`:** populate real `usage` (prompt/completion/total) from the turn's token accounting
  (the same numbers `cost-v1` reports over A2A) so OpenAI-SDK clients + cost tooling work.
- **Operation catalog:** a generated `GET /api/operations` (and `protoagent config
  operations`) enumerating every D2 op with its params + which surfaces expose it — the "one
  operation, three projections" made introspectable, and the source the CLI help + MCP tool
  list derive from.

### E. Local-LLM onboarding + "use in app"

protoAgent already accepts any OpenAI-compatible `api_base`, so onboarding is discovery + a
one-liner, not new model plumbing:

- **Discovery** — `protoagent setup` (and `protoagent model discover`) probe well-known local
  endpoints — Ollama (`http://localhost:11434/v1`, `/api/tags`), LM Studio (`:1234/v1`), vLLM /
  HF TGI (`/v1/models`) — and list the models each serves (mirrors proto's `modelDiscovery`).
- **`protoagent model` verbs** — `list` (configured + discovered), **`use`** (the load-bearing
  one: `protoagent model use --base-url http://127.0.0.1:8080/v1 --model <id>` sets `api_base` +
  `model_name` + a placeholder key via the config op, non-interactively, no gateway required),
  `add`/`remove` (named endpoints), `pull <name>` (shell out to `ollama pull` when present). Must
  tolerate the HF quant-tag placeholder `:{{QUANT_TAG}}` (default to a sensible quant).
- **"Use in app" handoff — a HuggingFace local-app registration, not a deep-link** *(research
  2026-07-05)*. protoAgent is an OpenAI-compatible **agent runtime**, so it registers exactly
  like `hermes-agent`/`openclaw`/`pi`: a ~20-line PR to **`packages/tasks/src/local-apps.ts`** in
  `huggingface/huggingface.js`, gated on `isToolCallingLocalAgentModel` (GGUF/MLX + `conversational`
  + a tools chat-template), emitting a 3-step **snippet** (not a `deeplink`): (1) `getLocalServerStep`
  starts llama.cpp/MLX on `:8080/v1`, (2) `protoagent model use --base-url http://127.0.0.1:8080/v1
  --model ${modelId}`, (3) `protoagent` to launch. Once merged it shows on every compatible model
  card's "Use this model" dropdown. **Ollama** offers no such registry — it's OpenAI-API-only
  (`:11434/v1`), reached by the same `protoagent model use`. A `protoagent://` deep-link is only
  worth adding if we ship a GUI to foreground (optional, secondary). The prereqs the snippet needs
  are exactly this ADR's PR1 (installable command) + D5 (`model use` one-liner).
- **Leverage the HF quant reputation, not social clout.** Two compounding surfaces: (A) the
  registry PR puts protoAgent on *other* people's compatible cards; (B) — works today, no HF
  dependency — the team's own published **quant model cards** carry a "Run with protoAgent" snippet
  and `conversational` + tools-template metadata, so they auto-surface protoAgent the moment the PR
  merges. Converts at the moment of intent (a user on the card about to run the model).

### F. Install / distribution

Both, binary-first for the headline:

- **Zero-Python** — the existing PyInstaller **frozen binary** (the desktop build) shipped as
  `protoagent` behind `curl -fsSL https://…/install.sh | sh` (mirroring proto's `install.sh`:
  detect arch/OS, fetch, put on PATH, run `protoagent setup`). Also a Homebrew tap.
- **Python present** — `uv tool install protolabs-agent` / `pipx install protolabs-agent`
  (the PyPI name; `protoagent` is similarity-blocked) → the `protoagent` `console_scripts`
  entry on PATH.

### G. Auth

Unchanged. The CLI talks to a local instance with the resolved Bearer (from config/env); the
MCP admin profiles ride the same default-deny middleware and the ADR 0066 ceiling. No new
credential model.

## Rollout

- **PR1 — the `protoagent` CLI** (D1): entrypoint + subparser tree + `--help`, re-parent the
  five existing dispatchers, add `up`/`down`/`status`/`serve`/`setup`. Low risk (≈80%
  repackaging); immediately kills the "boring `python -m server`" front door.
- **PR2 — the `ops/` layer + MCP admin profiles** (D2 + D3): extract the shared ops, wire the
  three adapters, add profiles + the `ask_human` exclusion + `GET /api/mcp/exposed`.
- **PR3 — API tightening** (D4): namespace dedupe (aliased), `/v1` usage, `/api/operations`
  catalog.
- **PR4 — model onboarding** (D5): discovery + `protoagent model` + the install scripts; the
  `protoagent://` deep-link + "use in app" as a fast-follow once its payload contract lands.

Install/distribution (F) rides PR1 (binary + `uv tool`) and PR4 (the `install.sh` polish).

## Consequences

- **One terminal identity.** `protoagent up && protoagent plugin install … && protoagent
  fleet up` replaces a grab-bag of `python -m server`, `uv`, and docker — and it's
  discoverable via `--help`.
- **"Operate over MCP" becomes true** — a foreign brain can administer a protoAgent within an
  explicit, consented profile, not just chat with it.
- **The surfaces stop drifting** — a new operation is one `ops/` function that appears on all
  three surfaces by construction, listed in one catalog.
- **Breaking API changes land once** (aliased a release) instead of accreting a third idiom.
- **Local models are a click** — the Ollama/HF ecosystems can hand a model to protoAgent.
- Cost: a real `ops/` seam + adapters is more indirection than three ad-hoc surfaces; the
  payoff is the anti-drift guarantee.

## Open questions

1. **HF local-app PR — acceptance + gate choice (D5).** *(Resolved mechanism, open on execution.)*
   The registration is a PR to `huggingface.js` `local-apps.ts` (review ≈ 1–2 wk, no formal bar,
   GGUF/MLX is the ticket). Open: use the narrow `isToolCallingLocalAgentModel` gate (the honest
   agent-runtime precedent — reviewers will steer here) vs. the broad `isLlamaCppGgufModel` (all
   ~45K GGUF cards, but over-reaching draws a change request)? And do we also ship a GUI
   `protoagent://` deep-link, or snippet-only like the other three agent runtimes (snippet-only)?
2. **CLI ↔ running-server vs on-disk ops.** Some ops act on disk (plugin install), some need a
   live server (fleet status, hot config). Does the CLL auto-boot a transient server for
   live-only ops, or require `protoagent up` first? (Lean: disk ops work headless; live ops
   error with "run `protoagent up`" unless `--ensure-up`.)
3. **`full` MCP profile blast radius.** A consented foreign brain doing `fleet up` / work
   assignment is powerful; do we cap it (rate/scope) beyond the consent ack, or trust the ADR
   0071 posture?
4. **Homebrew/npm namespace.** `protoagent` on Homebrew + a possible `@protolabsai/protoagent`
   thin npm shim (so `proto` users discover it) — worth it, or binary + `uv` only?

## Alternatives considered

- **Fold runtime management into `proto`** (the first instinct) — rejected: `proto` is a
  Qwen-fork terminal *coding agent* with its own identity; bolting a Python-runtime control
  plane onto it conflates two products and two audiences ("trips ourselves up"). The clean line
  is: `proto` talks-to / is-driven-by protoAgent over the wire; `protoagent` runs and manages
  the runtime.
- **Keep `python -m server <sub>`** — rejected: undiscoverable, un-installable, reads as an
  internal, and blocks the "one command" onboarding story.
- **One giant unified registry replacing REST + CLI + MCP** — rejected as over-reach: the two
  existing spines (tool registry, `graph/**` cores) are already sound; the fix is a thin `ops/`
  seam over the admin spine + faithful projections, not a rewrite.
- **A LiteLLM-gateway-only model story** — rejected for local onboarding: the gateway stays the
  default, but requiring it for "point at my Ollama" is friction the direct `api_base` path
  already avoids.
