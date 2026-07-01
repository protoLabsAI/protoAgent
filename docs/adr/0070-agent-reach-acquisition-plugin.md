# 0070 — Agent Reach as an acquisition-layer plugin (multi-platform source connectors)

Status: **Proposed**

Relates to: [0001](0001-extensibility-and-plugin-architecture.md) (plugins),
[0005](0005-tool-pollution-and-progressive-disclosure.md) (tool pollution),
[0011](0011-deep-research-workflow.md) (deep-research workflow),
[0018](0018-plugin-surfaces-routes-subagents.md) (plugin subagents/surfaces),
[0019](0019-plugin-config-settings-secrets.md) (plugin config/secrets),
[0060](0060-skill-progressive-disclosure.md) (skill progressive disclosure).
Investigates [#1559](https://github.com/protoLabsAI/protoAgent/issues/1559).

## Context

A protoAgent doing research today has exactly two generic web primitives, both in
`tools/lg_tools.py`:

- **`web_search`** — DuckDuckGo via `ddgs` (free, no key), returns a plain-text hit list.
- **`fetch_url`** — `httpx` GET + BeautifulSoup → **plain text**. No JavaScript execution,
  no auth/cookies, no readability-quality markdown; SSRF/egress-guarded (ADR 0008).

Everything above that — the `researcher`/`antagonist`/`verifier`/`synthesizer` subagents
(`graph/subagents/config.py`), the ADR 0011 `deep-research` workflow recipe, the
`web-research` skill, and the ingestion pipeline (`ingestion/engine.py`: web / YouTube
transcript / PDF / audio / video) — is **built on those two primitives**. So the *synthesis*
layer is strong, but the *acquisition* layer is thin: content behind an auth wall, a JS app,
or a platform API (Reddit, X/Twitter, Hacker News, YouTube-as-a-tool, Bilibili, Xiaohongshu,
GitHub search, RSS) is reachable only as best-effort generic `fetch_url` plain text, or not
at all.

[Agent Reach](https://github.com/Panniantong/Agent-Reach) (MIT) is purpose-built for exactly
this gap: a **self-healing skill + a bag of platform CLIs** (`curl r.jina.ai` for readability
markdown, `yt-dlp`, `bili-cli`, `gh`, Exa via `mcporter`, `twitter-cli`, `rdt-cli`, OpenCLI).
Each platform is an *ordered list of primary + fallback backends*; `agent-reach doctor` health-
checks them and `agent-reach update` swaps dead routes (e.g. it silently moved Bilibili off
`yt-dlp` when it was blocked). Cookies live locally in `~/.agent-reach/config.yaml` (mode 600),
never uploaded. It runs headless on a server (proxy ~$1/mo). **Notably, its integration model —
"the agent shells out to a CLI" — is the same one the protoLabs [`rabbit-hole.io`](https://github.com/protoLabsAI/rabbit-hole.io)
deep-research product already uses for the fleet (`rh` CLI).**

The question ([#1559](https://github.com/protoLabsAI/protoAgent/issues/1559)) is *where this fits*.
The answer is that Agent Reach is **not a "deep research" engine** — protoAgent already has that
(ADR 0011, itself informed by rabbit-hole.io). It is the **source-acquisition layer that feeds
deep research and RAG**. Treating it as a research engine would duplicate ADR 0011; treating it
as the acquisition layer fills a real gap without overlap.

## Decision

Ship Agent Reach as an **opt-in acquisition-layer plugin** (`plugins/agent-reach`, off by
default), scoped to source connectors — *not* a synthesis/research surface. Use the seams that
already exist; add no core edits.

### D1 — A skill, not fifteen tools (avoid roster pollution, ADR 0005)

The plugin bundles a `SKILL.md` via `register_skill_dir` (ADR 0060 progressive disclosure) that
carries the "which reader for which platform, and how to fall back" guidance — the same shape
Agent Reach already ships as a skill, and the same reason ADR 0005 exists: fifteen always-on
platform tools would swamp the binding layer. The skill loads on demand via `load_skill`.

### D2 — A small typed-tool surface (the high-value few)

On top of the skill, expose a **handful** of typed tools via `register_tool`, led by the
zero-config, highest-leverage one:

- **`reach_read(url) → clean markdown`** — any URL through the readability backend. This alone
  upgrades every research surface: it is a strictly better `fetch_url` (markdown vs stripped
  text, JS-rendered vs raw HTML) and a higher-fidelity feed into `knowledge_ingest`.
- **`reach_search(query, platform)`** — for the top few platforms only (Reddit, X, YouTube,
  GitHub, Hacker News). Returns structured results.

Implementation is a **shell-out to the `agent-reach`/platform CLIs**, gated by the existing
`run_command` policy — mirroring how the fleet shells out to `rh`. A single `reach(args…)`
escape-hatch tool is acceptable as an alternative to the typed set, but the typed surface reads
better in the tool roster and in delegate tool-lists (D4).

### D3 — Cookies and API keys as plugin secrets (ADR 0019)

Per-platform cookies and keys are declared in `protoagent.plugin.yaml`
(`secrets: [x_cookie, reddit_cookie, …]`, routed to the untracked `secrets.yaml`) and surfaced
in **Settings** like any other plugin secret. Zero-config platforms (web, YouTube, GitHub, RSS)
work with no secrets at all; auth platforms are opt-in. `agent-reach doctor` is exposed as a
**plugin status probe** (ADR 0018) so the console shows which platforms are currently live.

### D4 — Wire into deep research + RAG, don't reinvent it

The acquisition tools are added to the `researcher`/`antagonist` subagent tool-lists (or via
`register_late_tool_factory` so they compose with the full toolset), so the ADR 0011 workflow
gains platform reach *for free*. `reach_read → knowledge_ingest` feeds the hybrid-RRF store.
No new research pipeline is introduced.

### D5 — Relationship to rabbit-hole.io: complementary layers, one optional bundle

Agent Reach (breadth of sources) and rabbit-hole `rh` (depth of synthesis: `rh research` →
cited report) sit on opposite ends of the same pipeline and share the shell-out model. Two
supported shapes:

- **Lean (default):** `agent-reach` acquisition + protoAgent's own ADR 0011 deep-research. No
  new heavy dependency.
- **Full (optional):** add a `rabbit-hole` delegate that shells out to `rh` for heavy synthesis,
  giving a "deep research over any platform" bundle. Deferred to a later slice / separate ADR.

## Consequences

- **Gain:** platform reach behind auth/JS walls; readability markdown; a better `fetch_url` and
  a better RAG feed — with the resilient primary/fallback + `doctor` posture that matches
  protoAgent's own reliability ethos.
- **Cost:** the plugin pulls a heavy runtime tail (Python + Node + `yt-dlp`/`gh`/`bili-cli`/…),
  so it stays **opt-in and is best shipped in its own container image**, never in lean core.
- **Operational/legal surface:** cookie management and social-platform ToS/ban risk are real;
  auth platforms are gated behind explicit config and documented with the "secondary account"
  warning Agent Reach itself gives.
- **Redundancy risk:** `reach_read` overlaps `fetch_url`. Mitigated by skill guidance —
  *reach for platform/auth/JS pages, `fetch_url` for generic ones* — and by making `reach_read`
  a genuine upgrade rather than a parallel path.

## Alternatives considered

- **Wrap it as a managed MCP server** (`register_mcp_server`, ADR 0019) instead of shell-out
  tools. Viable and cleaner for auth, but Agent Reach is CLI-native and self-updating; an MCP
  wrapper fights its `doctor`/`update` model and duplicates its routing. Revisit if a
  first-class Agent-Reach MCP appears upstream.
- **Fifteen per-platform tools, always on.** Rejected — ADR 0005 tool pollution.
- **Treat it as a deep-research engine / replace ADR 0011.** Rejected — it has no synthesis
  layer; it is acquisition. Duplicating ADR 0011 would be a regression.
- **Do nothing; rely on `fetch_url`.** Rejected — generic fetch cannot read auth/JS/platform
  content, which is a growing share of useful sources.

## Slices

- **PR1 — Phase 1 (high value, low risk):** `reach_read` (URL → markdown, zero-config) +
  bundled `SKILL.md` + `agent-reach doctor` status probe + `reach_read → knowledge_ingest`.
  Ships as an opt-in plugin with its own container. Proves the acquisition layer end-to-end.
- **PR2 — Phase 2 (auth platforms):** `reach_search` for Reddit/X/YouTube/GitHub/HN + the
  cookie/key secrets seam (ADR 0019) + Settings surface. Carries the ops/ban risk; gated.
- **PR3 — Phase 3 (optional, own ADR):** the `rabbit-hole` `rh` synthesis delegate for the full
  "deep research over any platform" bundle (D5, Full).
