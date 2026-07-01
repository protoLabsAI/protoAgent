import { defineConfig } from "vitepress";

// GitHub Pages serves under /protoAgent/; the Cloudflare marketing build folds
// the docs in at /docs/ (DOCS_BASE=/docs/). Env-driven so both coexist.
const base = process.env.DOCS_BASE || "/protoAgent/";

export default defineConfig({
  title: "protoAgent",
  description:
    "Template repository for building protoLabs A2A agents on LangGraph.",
  base,

  // The protoLabs.studio theme (@protolabsai/vitepress-theme) is dark-first like
  // the marketing site; pin the dark, brand-first ground.
  appearance: "force-dark",

  // Tutorials legitimately reference the local dev server (http://localhost:7870);
  // VitePress treats dead links as fatal, so skip just the localhost ones.
  ignoreDeadLinks: "localhostLinks",

  // docs/dev/ is the team's internal engineering area (handoffs + notes) — it
  // lives in the repo (committed, shared) but is NOT part of the published site.
  srcExclude: ["dev/**"],

  head: [
    ["link", { rel: "icon", type: "image/svg+xml", href: `${base}favicon.svg` }],
    // Social cards — canonical absolute image (the dark protoAgent banner) so it
    // resolves regardless of which base the build serves under.
    ["meta", { property: "og:type", content: "website" }],
    ["meta", { property: "og:title", content: "protoAgent" }],
    ["meta", { property: "og:image", content: "https://agent.protolabs.studio/docs/protoagent-banner.png" }],
    ["meta", { name: "twitter:card", content: "summary_large_image" }],
    ["meta", { name: "twitter:image", content: "https://agent.protolabs.studio/docs/protoagent-banner.png" }],
  ],

  themeConfig: {
    logo: "/favicon.svg",

    nav: [
      { text: "Tutorials", link: "/tutorials/" },
      { text: "Guides", link: "/guides/" },
      { text: "Reference", link: "/reference/" },
      { text: "Explanation", link: "/explanation/" },
    ],

    // Each Diátaxis section's sidebar is grouped by the SAME domain taxonomy
    // (Getting started → Agent core → Skills → Knowledge → A2A/fleet → Tools/plugins
    // → Console → Operate → Forks), so "where does X live" reads the same everywhere.
    // A section only lists the domains it actually has pages for.
    sidebar: {
      "/tutorials/": [
        { text: "Tutorials", items: [{ text: "Overview", link: "/tutorials/" }] },
        {
          text: "Getting started",
          collapsed: false,
          items: [{ text: "Spin up your first agent", link: "/tutorials/first-agent" }],
        },
        {
          text: "Skills, subagents & workflows",
          collapsed: false,
          items: [{ text: "Write your first skill", link: "/tutorials/first-skill" }],
        },
        {
          text: "Tools, MCP & plugins",
          collapsed: false,
          items: [{ text: "Write your first tool", link: "/tutorials/first-tool" }],
        },
      ],

      "/guides/": [
        { text: "How-To Guides", items: [{ text: "Overview", link: "/guides/" }] },
        {
          text: "Getting started",
          collapsed: false,
          items: [
            { text: "Fork the template (fast path)", link: "/guides/fork-the-template" },
            { text: "Customize & deploy", link: "/guides/customize-and-deploy" },
          ],
        },
        {
          text: "Agent core & runtime",
          collapsed: false,
          items: [
            { text: "Goal mode", link: "/guides/goal-mode" },
            { text: "Watches", link: "/guides/watches" },
            { text: "File GitHub issues (/issue)", link: "/guides/file-github-issues" },
            { text: "Schedule future work", link: "/guides/scheduler" },
            { text: "Middleware", link: "/guides/middleware" },
            { text: "Run on a coding agent (ACP runtime)", link: "/guides/acp-runtime" },
          ],
        },
        {
          text: "Skills, subagents & workflows",
          collapsed: false,
          items: [
            { text: "Skills (SKILL.md)", link: "/guides/skills" },
            { text: "Add a custom skill (A2A card)", link: "/guides/add-a-skill" },
            { text: "Configure subagents", link: "/guides/subagents" },
            { text: "Reusable workflows", link: "/guides/workflows" },
          ],
        },
        {
          text: "Knowledge & memory",
          collapsed: false,
          items: [
            { text: "Ingest documents & media", link: "/guides/ingestion" },
            { text: "Tune the knowledge store (RAG)", link: "/guides/knowledge" },
          ],
        },
        {
          text: "A2A, fleet & delegates",
          collapsed: false,
          items: [
            { text: "Delegates (agents & endpoints)", link: "/guides/delegates" },
            { text: "Spawn CLI coding agents (ACP)", link: "/guides/coding-agents" },
            { text: "Verifier-grounded coder (coder_solve)", link: "/guides/coder" },
            { text: "Fleet (many agents on one host)", link: "/guides/fleet" },
            { text: "Portfolio (one PM across boards)", link: "/guides/portfolio" },
          ],
        },
        {
          text: "Tools, MCP & plugins",
          collapsed: false,
          items: [
            { text: "Connect MCP servers", link: "/guides/mcp" },
            { text: "Plugins", link: "/guides/plugins" },
            { text: "Building a plugin view", link: "/guides/building-react-plugin-views" },
            { text: "Build a communication plugin", link: "/guides/communication-plugins" },
            { text: "Install & publish plugins (git URLs)", link: "/guides/plugin-registry" },
            { text: "Discord surface", link: "/guides/discord" },
          ],
        },
        {
          text: "Console & UI",
          collapsed: false,
          items: [
            { text: "Operator console (React/Tauri)", link: "/guides/react-tauri-ui" },
            { text: "Command palette (⌘K)", link: "/guides/command-palette" },
            { text: "Access from your phone (LAN / Tailscale)", link: "/guides/phone-access" },
            { text: "Run headless (API + A2A)", link: "/guides/headless" },
          ],
        },
        {
          text: "Operate & deploy",
          collapsed: true,
          items: [
            { text: "Deploy via GHCR", link: "/guides/deploy" },
            { text: "Deploy in Docker (seed + UI override)", link: "/guides/deploy-docker" },
            { text: "Releasing", link: "/guides/releasing" },
            { text: "Run multiple instances", link: "/guides/multi-instance" },
            { text: "Sandboxing & egress", link: "/guides/sandboxing" },
            { text: "Wire Langfuse + Prometheus", link: "/guides/observability" },
          ],
        },
        {
          text: "Forks & evals",
          collapsed: true,
          items: [
            { text: "Build an operator fork (Roxy)", link: "/guides/operator-fork" },
            { text: "Sync a fork from upstream", link: "/guides/upstream-sync" },
            { text: "Eval your fork", link: "/guides/evals" },
          ],
        },
      ],

      "/reference/": [
        { text: "Reference", items: [{ text: "Overview", link: "/reference/" }] },
        {
          text: "Agent core & runtime",
          collapsed: false,
          items: [
            { text: "Configuration", link: "/reference/configuration" },
            { text: "Environment variables", link: "/reference/environment-variables" },
          ],
        },
        {
          text: "Skills, subagents & workflows",
          collapsed: false,
          items: [{ text: "Skills (SKILL.md)", link: "/reference/skills" }],
        },
        {
          text: "A2A, fleet & delegates",
          collapsed: false,
          items: [
            { text: "A2A endpoints", link: "/reference/a2a-endpoints" },
            { text: "Agent card", link: "/reference/agent-card" },
            { text: "Extensions", link: "/reference/extensions" },
          ],
        },
        {
          text: "Tools, MCP & plugins",
          collapsed: false,
          items: [{ text: "Starter tools", link: "/reference/starter-tools" }],
        },
        {
          text: "Console & UI",
          collapsed: false,
          items: [{ text: "Operator REST API", link: "/reference/operator-api" }],
        },
      ],

      "/explanation/": [
        { text: "Explanation", items: [{ text: "Overview", link: "/explanation/" }] },
        {
          text: "Agent core & runtime",
          collapsed: false,
          items: [
            { text: "Architecture", link: "/explanation/architecture" },
            { text: "Output protocol", link: "/explanation/output-protocol" },
            { text: "Mid-turn steering", link: "/explanation/steering" },
            { text: "LiteLLM gateway", link: "/explanation/litellm-gateway" },
          ],
        },
        {
          text: "Knowledge & memory",
          collapsed: false,
          items: [{ text: "Memory & knowledge store", link: "/explanation/memory-and-knowledge" }],
        },
        {
          text: "A2A, fleet & delegates",
          collapsed: false,
          items: [
            { text: "A2A protocol", link: "/explanation/a2a-protocol" },
            { text: "Cost & trace propagation", link: "/explanation/cost-and-trace" },
          ],
        },
        {
          text: "Operate & deploy",
          collapsed: false,
          items: [
            { text: "Security & trust model", link: "/explanation/security-and-trust" },
            { text: "Tuning & cost", link: "/explanation/tuning-and-cost" },
          ],
        },
        {
          text: "Architecture decisions",
          collapsed: false,
          items: [{ text: "ADRs", link: "/adr/" }],
        },
      ],
    },

    socialLinks: [
      { icon: "github", link: "https://github.com/protoLabsAI/protoAgent" },
    ],

    search: {
      provider: "local",
    },

    footer: {
      message: "Part of the protoLabs autonomous development studio.",
      copyright: "© 2026 protoLabs.studio",
    },
  },
});
