# ADR 0001 — Extensibility & Plugin Architecture

- **Status:** Accepted (2026-05-29) — execution underway, slice by slice
- **Date:** 2026-05-29
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, extensibility, plugins, skills, tools, mcp, marketplace, security
- **Supersedes / Superseded by:** —

> Accepted. This ADR records the decision to make protoAgent user-extensible
> (tools, subagents, skills, personas) by adopting the converging standards
> (AgentSkills, MCP) over bespoke formats, delivered in independently-shippable
> slices. Progress: ✅ Slice 1 (`SKILL.md` skills, #251), ✅ Slice 2 (MCP client,
> #254), ✅ agent-emitted-skill persistence (#256), ✅ Slice 4 (plugin system,
> #258). Remaining: Slice 5 (registry/marketplace index). Slice 3's tool-registry
> refactor was folded into the MCP/plugin `extra_tools` seam; capability
> *enforcement* (declared-but-not-enforced today) is a follow-up.

---

## 1. Context & Problem Statement

protoAgent is a single-fork-per-agent template: capabilities are added by
editing source (`tools/lg_tools.py`, `graph/subagents/config.py`,
`config/soul-presets/`) and rebuilding. That is fine for an engineer forking
the template, but it blocks a larger goal: letting **users** extend a running
agent with new **tools, subagents, skills, and personas** — ideally installable
from a shared **marketplace** — without forking or recompiling.

Two questions this ADR answers:

1. What is the right *architecture* for third-party extensibility in protoAgent?
2. How much of it should be invented vs. adopted from the converging industry
   standards (AgentSkills, MCP) and the reference agent stacks we already track
   (Hermes, OpenClaw, Pi)?

**Non-goals:** committing to a delivery timeline; building a marketplace
backend; redesigning the LangGraph core. The marketplace is explicitly the
*last* and *cheapest* piece (an index over manifests), not the first.

---

## 2. Decision Drivers

- **Standards alignment.** Re-inventing a skill or tool format would strand
  protoAgent off the AgentSkills / MCP ecosystem that Hermes, OpenClaw, Claude
  Code, Codex, and Cursor all interoperate with.
- **Security first.** Third-party code is untrusted by default. The trust
  boundary must be explicit and enforced, not assumed.
- **Reuse existing seams.** protoAgent already has registries, a middleware
  hook layer, a self-emitted skills store, an A2A extension convention, audit +
  Langfuse observability, and a scrubbed-subprocess sandbox (`execute_code`).
  The design should *formalize* these, not replace them.
- **Opt-in, not auto-on.** Installing an extension must not silently expand the
  agent's capability surface; enabling is a deliberate act.
- **Operator legibility.** Everything an extension contributes must be visible
  in the operator console and the audit trail.
- **Low core coupling.** Extension authors should not need to understand
  LangGraph internals to ship a tool.

---

## 3. Current State (what protoAgent already has)

| Capability | Today | Shape |
|---|---|---|
| **Tools** | `tools/lg_tools.py::get_all_tools(knowledge_store, scheduler)` | Static list; backend-bound subsets (`MEMORY_TOOL_NAMES`, `SCHEDULER_TOOL_NAMES`). No dynamic registration. |
| **Subagents** | `graph/subagents/config.py::SUBAGENT_REGISTRY` | Static `SubagentConfig` dataclasses (name, prompt, tool allowlist, `disallowed_tools`, `max_turns`, `allow_skill_emission`). |
| **Hooks / lifecycle** | `graph/agent.py::_build_middleware()` | Config-gated list of langchain `AgentMiddleware` (PromptCache, Knowledge, Audit, Memory, Ingest, Enforcement, Summarization, ModelFallback, MessageCapture). |
| **Skills (auto)** | `graph/extensions/skills.py` + `graph/skills/` (FTS5 index + curator) | **Agent self-authors** skill-v1 "recipes" via `task(emit_skill=True)`, indexed for retrieval. The hard half of procedural memory already exists. |
| **Personas** | `config/soul-presets/*.md` | Markdown SOUL presets; no frontmatter / discovery metadata. |
| **Tool gating** | `EnforcementMiddleware` (`enforcement_disallowed_tools`, rate limits) | Deny-list + per-tool rate limit. Allowlist is per-subagent. |
| **Extension convention** | A2A `DataPart` + `mimeType` (`cost-v1`, `worldstate-delta-v1`, `skill-v1`) | Established pattern for typed, routed payloads. |
| **Untrusted code** | `execute_code` | Model-authored Python in a subprocess with scrubbed env (no secrets) + hard timeout + fd-RPC tool bridge. |
| **Config** | `langgraph-config.yaml` (live, untracked) + `secrets.yaml` overlay + `PROTOAGENT_CONFIG_DIR` | Per-deployment, wizard/drawer-editable. |
| **MCP client** | **None** | Gap vs. every reference stack. |
| **Dynamic discovery / install** | **None** | Gap. |
| **Marketplace** | **None** | Gap. |

**Read:** protoAgent already owns the rare, hard parts (self-emitted skills,
middleware hooks, audit, an extension convention, a code sandbox). What's
missing is the *plumbing* that makes those user-facing: dynamic registration,
a manifest, a loader, MCP, and an index.

---

## 4. Prior Art & Research

Three independent stacks (Hermes, OpenClaw) plus the open standards (AgentSkills,
MCP) have **converged on the same three-tier model**.

### 4.1 The convergent model

| Tier | Definition | Format | Trust posture |
|---|---|---|---|
| **Tools** | Low-level callable primitives | Typed fn + JSON schema | Allowlist / policy-gated |
| **Skills** | "How & when to use tools" — workflows | **`SKILL.md`** folder (YAML frontmatter + markdown body) | Env/binary-filtered; trust labels |
| **Plugins** | Code packages adding tools/hooks/providers | Manifest + entrypoint + lifecycle hooks | Opt-in, disabled by default |

A **marketplace** is an index over published manifests with trust labels — not
heavy infrastructure.

### 4.2 Hermes Agent (Nous Research) — *Python, self-improving*

- Plugins: `~/.hermes/plugins/<name>/` + project dir + pip `hermes_agent.plugins`
  entry points. `plugin.yaml` manifest (`name`, `version`, `description`,
  `requires_env`); `register(ctx)` → `ctx.register_tool / register_hook /
  register_command / register_skill`.
- Deep lifecycle hooks: `pre/post_tool_call`, `pre/post_llm_call`,
  `on_session_start/end/reset`, `subagent_stop`, `pre_gateway_dispatch`.
- Plugin *kinds* with cardinality (general multi; memory provider single;
  context engine single; model provider multi). Disabled by default.
- Skills are **agent-authored procedural memory** (`skill_manage`) *and*
  installable from registries with **Builtin / Trusted / Community** trust
  labels. MCP is first-class.

### 4.3 OpenClaw — *TypeScript/npm, marketplace-first*

- Plugins: npm ES-module packages. `package.json` `openclaw` key
  (`extensions`, `compat.pluginApi` semver) **+** `openclaw.plugin.json`
  (`id`, `contracts.tools`, `activation.onStartup`, `configSchema`).
- **Lazy discovery by contract:** tools declared in `contracts.tools` so the
  host knows ownership *without loading every plugin runtime*.
- Tool gating: required-by-default or `{ optional: true }` → user opts in via
  `tools.allow`. Core-name collisions skipped + logged.
- Skills = AgentSkills `SKILL.md` folders, filtered at load by OS/config/binary
  presence. Marketplace = **ClawHub** (`clawhub:org/plugin`), plus
  Codex/Claude/Cursor-compatible bundles.

### 4.4 AgentSkills open standard (Anthropic)

- A skill is a **folder with `SKILL.md`**: YAML frontmatter + markdown body.
  Required: `name` (lowercase-hyphen) and `description` (≤1024 chars, written
  "pushy" to combat under-triggering — the primary trigger signal).
- No format restriction on the body. Supports progressive disclosure (bundle
  scripts/resources alongside). This is the format **both reference stacks and
  Claude Code itself** use → maximum portability.

### 4.5 MCP (Model Context Protocol)

- Client–server over JSON-RPC 2.0. Three primitives: **Tools** (actions),
  **Resources** (readable data), **Prompts** (templates). Transports: **stdio**
  (local) and **Streamable HTTP** (remote, OAuth 2.1).
- 2026 roadmap: stateless HTTP core, **Tasks** (long-running work),
  **MCP Apps** (server-rendered UI). It is the de-facto seam for *external*
  tools — adopting it means protoAgent inherits an existing server ecosystem
  rather than asking authors to write protoAgent-specific tools.

### 4.6 Sandboxing consensus

- **Shared-kernel containers are insufficient** for untrusted AI-authored code.
  Real isolation = gVisor (syscall interception) or **microVMs** (Firecracker /
  E2B).
- Four mandatory boundary layers: **network egress, filesystem scope, secrets
  scoping, config-file protection.** A sandboxed extension that tries to read
  SSH keys or phone home fails because the paths aren't mounted and the egress
  isn't allowlisted.
- protoAgent's `execute_code` already embodies the cheap version (scrubbed env,
  timeout, no secret access); third-party *plugin* code needs the same or
  stronger, plus an explicit capability grant.

---

## 5. Considered Options

### Option A — Status quo (informal registries, fork to extend)
Keep editing source. **Rejected** as the stated goal is user extensibility.
- ➕ Zero work, full control. ➖ No user extensibility, no marketplace, every
  capability change is a rebuild.

### Option B — Config-only extensibility
Expand the YAML so operators can *register* external tools/subagents
declaratively (e.g. an MCP server URL, or a subagent prompt) — no code packages.
- ➕ Low risk, no code-loading, leans on the existing config system. ➖ Can't
  ship genuinely new *code* tools; limited to what's declaratively expressible;
  no skills/personas distribution.

### Option C — Full tiered plugin system + MCP + marketplace (phased) ✅ **Recommended**
Adopt the convergent three-tier model, grounded in protoAgent's seams, delivered
in phases (MCP and SKILL.md first — both standards-based and high-leverage).
- ➕ Standards-aligned, future-proof, reuses existing hooks/skills/sandbox,
  marketplace falls out cheaply at the end. ➖ Largest surface; security work is
  real; needs a stable plugin API + versioning.

### Option D — MCP-only
Lean entirely on MCP for third-party tools; skills/personas stay first-party.
- ➕ Smallest new surface, instant external-tool ecosystem, no bespoke loader.
  ➖ No story for shippable *skills/personas/subagents/middleware*; MCP doesn't
  cover protoAgent's richer extension points (hooks, self-skills, soul presets).

**Decision:** **Option C, phased**, where **Phase 1 is essentially Option D
(MCP) + adopting `SKILL.md`** — so the high-value, standards-based wins land
first and the bespoke plugin loader/marketplace come only if warranted.

---

## 6. Decision Outcome

Adopt a **four-tier extensibility model** plus a thin registry, aligned to the
industry convergence and mapped onto protoAgent's existing components.

### 6.1 Tier 1 — Tools (dynamic registry + capability gating)
- Replace the static `get_all_tools()` with a **tool registry** that supports
  (a) built-in tools, (b) plugin-contributed tools, and (c) MCP-proxied tools,
  all behind one interface.
- **Capability gating** extends today's `EnforcementMiddleware`: third-party
  tools are **optional/off by default**; an operator allowlist (per the existing
  `enforcement`/subagent allowlist machinery) opts them in. Name collisions with
  core tools are rejected and logged (OpenClaw's rule).
- Every plugin tool inherits Audit + Langfuse (it routes through the same
  middleware), so the audit trail stays complete.

### 6.2 Tier 2 — Skills (adopt the AgentSkills `SKILL.md` standard)
- **Human-authored / installable skills** use the open `SKILL.md` folder format
  (frontmatter `name` + `description`, markdown body, optional
  `requires`/env/OS gating à la OpenClaw). Loaded + filtered at startup.
- **Keep the existing skill-v1 self-emission** as a complementary
  *auto-authored* track (procedural memory). Bridge: a curated self-emitted
  skill can be "promoted" to a `SKILL.md` for sharing. This is protoAgent's
  differentiator — *both* human-authored and agent-authored skills in one store.
- **Personas become skills/presets in the same folder convention** (a SOUL
  preset is a `SKILL.md`-shaped doc with a `persona` kind), so they're
  discoverable and installable like everything else.

### 6.3 Tier 3 — Plugins (manifest + drop-in dir + entry points)
- A plugin is a directory with a **manifest** declaring what it contributes and
  a Python entry module exposing `register(ctx)`. Discovery: a plugins dir under
  `PROTOAGENT_CONFIG_DIR` (+ Python `protoagent.plugins` entry points, the
  Hermes pattern). **Disabled by default**; enabled via config/console.
- Contributions (the protoAgent extension points): `tools`, `subagents`,
  `middleware` (the hook layer — pre/post tool & model, session lifecycle),
  `skills`, `personas`, and providers (`model`, `scheduler`, `knowledge`).
- **Lazy by manifest** (OpenClaw's `contracts`): the host learns ownership from
  the manifest without importing every plugin's code.

Illustrative manifest (design sketch, not an implementation):

```yaml
# protoagent.plugin.yaml
id: github-tools
name: GitHub Tools
version: 0.1.0
api: ">=1.0,<2.0"          # plugin-API compat (semver), à la OpenClaw compat.pluginApi
requires_env: [GITHUB_TOKEN]
trust: community            # builtin | trusted | community
contributes:
  tools: [gh_search_issues, gh_open_pr]
  middleware: [pre_tool_call]      # lifecycle hooks it registers
  skills: [triage-issue]           # bundled SKILL.md folders
capabilities:               # the capability grant the operator must approve
  network: ["api.github.com"]
  filesystem: "none"
  secrets: [GITHUB_TOKEN]
activation:
  enabled: false            # opt-in
```

### 6.4 Tier 4 — MCP client (external interop)
- Add an **MCP client** so any MCP server (stdio or Streamable HTTP) maps its
  Tools/Resources/Prompts into protoAgent's tool registry. Configured in YAML
  (`mcp.servers[]`), gated by the same allowlist. This is the **highest-leverage
  early phase**: it imports an entire external ecosystem with no bespoke SDK.

### 6.5 Registry / "marketplace" (thin, last)
- A **registry is an index over published manifests** with trust labels
  (Builtin / Trusted / Community — Hermes' model). "Install" = fetch + drop into
  the plugins dir (or add an `mcp.servers[]` entry / a `SKILL.md` folder).
- Reuse protoAgent's existing distribution gravity (git, A2A) before building
  anything custom. Cross-compat with the AgentSkills ecosystem means many
  skills are installable on day one.

### 6.6 Security & trust (cross-cutting, non-negotiable)
- **Trust tiers:** `builtin` (first-party) > `trusted` (signed/reviewed) >
  `community` (untrusted). Untrusted defaults to the most restrictive grant.
- **Capability grants:** a plugin declares the network/filesystem/secrets it
  needs; the operator approves explicitly at enable time (console surfaces it).
  No declared capability → denied at runtime.
- **Code isolation:** lean on the `execute_code` posture (scrubbed env, timeout)
  for in-process tool code; for genuinely untrusted plugins, document that
  shared-kernel isolation is insufficient and gate heavier isolation
  (gVisor/microVM, or MCP-over-HTTP to an external server) behind the
  `community` tier. **Prefer pushing untrusted tools out-of-process via MCP** so
  the protoAgent process never imports third-party code.
- **Secrets:** plugins never read `secrets.yaml`; they receive only their
  declared `requires_env`, injected at spawn (mirrors `execute_code`).
- **Audit:** every plugin tool call lands in `audit.jsonl` + Langfuse, tagged
  with the plugin id and trust tier.

---

## 7. Consequences

**Positive**
- protoAgent joins the AgentSkills + MCP ecosystem (portability, instant
  external tools, installable community skills).
- The rare assets (self-authored skills, middleware hooks, audit, sandbox) become
  user-facing instead of internal.
- Marketplace is cheap because it's just an index.
- Clear, enforced trust boundary replaces today's implicit "fork = trust."

**Negative / costs**
- A **stable plugin API** must be defined and versioned (breaking it breaks the
  ecosystem). `compat` semver is mandatory from day one.
- Security work is real and ongoing (capability enforcement, isolation,
  supply-chain review of community plugins).
- Dynamic loading adds startup cost, failure modes (a bad plugin must not crash
  boot), and observability surface.

**Neutral**
- The static registries (`get_all_tools`, `SUBAGENT_REGISTRY`) become the
  "builtin" tier behind the new registry interface — backward compatible.

---

## 8. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Untrusted plugin exfiltrates secrets / data | Capability grants + scrubbed env + prefer MCP-out-of-process; never expose `secrets.yaml`. |
| A broken plugin crashes startup | Load in isolation; a failing plugin is logged + disabled, never fatal (mirror the sidecar "log, don't crash" rule). |
| Plugin-API churn breaks the ecosystem | Semver `api`/`compat` field enforced at load; deprecation windows. |
| Tool-name collisions / shadowing core tools | Reject + log collisions (OpenClaw rule); namespace plugin tools by id. |
| Marketplace supply-chain (typosquats, malicious updates) | Trust tiers, signing for `trusted`, pinned versions, review for the curated index. |
| Over-triggering skills pollute context | AgentSkills progressive disclosure + the existing FTS relevance gating. |

---

## 9. Phasing (if pursued)

1. **MCP client** + adopt **`SKILL.md`** for skills/personas (standards-based,
   no bespoke loader, immediate ecosystem) — *highest leverage*.
2. **Tool registry** refactor (built-in + MCP behind one interface) + capability
   gating via `EnforcementMiddleware`.
3. **Plugin manifest + drop-in loader** (tools, middleware, subagents) with
   opt-in + trust tiers + audit tagging.
4. **Registry index** (trust labels, `install` = fetch+drop) + console UX for
   browse/enable/capability-approval.
5. **Heavier isolation tier** (gVisor/microVM) for `community` code, if demand
   justifies it.

Each phase is independently valuable and shippable; stop at any point.

---

## 10. Open Questions

- Plugin language: Python-only (matches Hermes + the core) vs. also WASM/MCP for
  language-agnostic, sandboxed extensions?
- Do self-emitted skill-v1 artifacts and `SKILL.md` skills share one store, or
  stay separate with a promotion bridge?
- Where does the curated registry live — a git index, an A2A-published catalog,
  or a hosted service? (Cheapest first.)
- Is there appetite for **MCP Apps** (server-rendered UI) inside the React
  console, or do plugins stay headless?
- Signing/attestation for the `trusted` tier — reuse an existing supply-chain
  tool or defer?

---

## 11. References

- [Anthropic — Agent Skills (engineering)](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) · [SKILL.md spec](https://github.com/anthropics/skills) · [agentskills.io spec](https://agentskills.io/specification)
- [Model Context Protocol — 2026 roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/) · [2026-07-28 spec RC](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [Hermes Agent — plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins) · [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) · [skills/tools/plugins (DeepWiki)](https://deepwiki.com/nousresearch-hermes-agent/hermes-agent/5-skills-tools-and-plugins)
- [OpenClaw — building plugins](https://docs.openclaw.ai/plugins/building-plugins) · [tools](https://docs.openclaw.ai/tools) · [creating skills](https://docs.openclaw.ai/tools/creating-skills) · [ClawHub ecosystem](https://open-claw.social/open-claw-ecosystem.html)
- Sandboxing: [Agent Sandbox (k8s SIG)](https://agent-sandbox.sigs.k8s.io/) · [best code-execution sandbox 2026 (Northflank)](https://northflank.com/blog/best-code-execution-sandbox-for-ai-agents)
- protoAgent internals: `tools/lg_tools.py` (`get_all_tools`), `graph/subagents/config.py` (`SUBAGENT_REGISTRY`), `graph/agent.py` (`_build_middleware`), `graph/extensions/skills.py` + `graph/skills/` (skill-v1), `graph/middleware/enforcement.py`, `config/soul-presets/`.
