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

    sidebar: {
      "/tutorials/": [
        {
          text: "Tutorials",
          items: [
            { text: "Overview", link: "/tutorials/" },
            { text: "Spin up your first agent", link: "/tutorials/first-agent" },
            { text: "Write your first tool", link: "/tutorials/first-tool" },
          ],
        },
      ],

      "/guides/": [
        {
          text: "How-To Guides",
          items: [{ text: "Overview", link: "/guides/" }],
        },
        {
          text: "Set up & deploy",
          collapsed: false,
          items: [
            { text: "Customize & deploy", link: "/guides/customize-and-deploy" },
            { text: "Fork checklist (fast path)", link: "/guides/fork-the-template" },
            { text: "Deploy via GHCR", link: "/guides/deploy" },
            { text: "Releasing", link: "/guides/releasing" },
          ],
        },
        {
          text: "Agent capabilities",
          collapsed: false,
          items: [
            { text: "Add a custom skill (A2A card)", link: "/guides/add-a-skill" },
            { text: "Skills (SKILL.md)", link: "/guides/skills" },
            { text: "Configure subagents", link: "/guides/subagents" },
            { text: "Reusable workflows", link: "/guides/workflows" },
            { text: "Goal mode", link: "/guides/goal-mode" },
            { text: "Schedule future work", link: "/guides/scheduler" },
          ],
        },
        {
          text: "Tools & coding agents",
          collapsed: false,
          items: [
            { text: "Connect MCP servers", link: "/guides/mcp" },
            { text: "Spawn CLI coding agents (ACP)", link: "/guides/coding-agents" },
            { text: "Run on a coding agent (ACP runtime)", link: "/guides/acp-runtime" },
            { text: "Delegates (agents & endpoints)", link: "/guides/delegates" },
          ],
        },
        {
          text: "Plugins & channels",
          collapsed: false,
          items: [
            { text: "Plugins", link: "/guides/plugins" },
            { text: "Plugin console views", link: "/guides/plugin-views" },
            { text: "Build a communication plugin", link: "/guides/communication-plugins" },
            { text: "Install & publish plugins (git URLs)", link: "/guides/plugin-registry" },
            { text: "Discord surface", link: "/guides/discord" },
            { text: "Google (Gmail + Calendar)", link: "/guides/google" },
          ],
        },
        {
          text: "Run & operate",
          collapsed: true,
          items: [
            { text: "Run headless (API + A2A)", link: "/guides/headless" },
            { text: "Operator console (React/Tauri)", link: "/guides/react-tauri-ui" },
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
        {
          text: "Reference",
          items: [
            { text: "Overview", link: "/reference/" },
            { text: "A2A endpoints", link: "/reference/a2a-endpoints" },
            { text: "Agent card", link: "/reference/agent-card" },
            { text: "Starter tools", link: "/reference/starter-tools" },
            { text: "Environment variables", link: "/reference/environment-variables" },
            { text: "Configuration", link: "/reference/configuration" },
            { text: "Extensions", link: "/reference/extensions" },
          ],
        },
      ],

      "/explanation/": [
        {
          text: "Explanation",
          items: [
            { text: "Overview", link: "/explanation/" },
            { text: "Architecture", link: "/explanation/architecture" },
            { text: "Memory & knowledge store", link: "/explanation/memory-and-knowledge" },
            { text: "A2A protocol", link: "/explanation/a2a-protocol" },
            { text: "Cost & trace propagation", link: "/explanation/cost-and-trace" },
            { text: "Tuning & cost", link: "/explanation/tuning-and-cost" },
            { text: "Output protocol", link: "/explanation/output-protocol" },
            { text: "LiteLLM gateway", link: "/explanation/litellm-gateway" },
            { text: "Architecture decisions (ADRs)", link: "/adr/" },
          ],
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
