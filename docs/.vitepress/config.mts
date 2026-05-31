import { defineConfig } from "vitepress";

export default defineConfig({
  title: "protoAgent",
  description:
    "Template repository for building protoLabs A2A agents on LangGraph.",
  base: "/protoAgent/",

  // Tutorials legitimately reference the local dev server (http://localhost:7870);
  // VitePress treats dead links as fatal, so skip just the localhost ones.
  ignoreDeadLinks: "localhostLinks",

  head: [["link", { rel: "icon", href: "/protoAgent/favicon.svg" }]],

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
          items: [
            { text: "Overview", link: "/guides/" },
            { text: "Customize & deploy", link: "/guides/customize-and-deploy" },
            { text: "Fork checklist (fast path)", link: "/guides/fork-the-template" },
            { text: "Add a custom skill", link: "/guides/add-a-skill" },
            { text: "Configure subagents", link: "/guides/subagents" },
            { text: "Reusable workflows", link: "/guides/workflows" },
            { text: "Skills (SKILL.md)", link: "/guides/skills" },
            { text: "MCP servers", link: "/guides/mcp" },
            { text: "Plugins", link: "/guides/plugins" },
            { text: "Goal mode", link: "/guides/goal-mode" },
            { text: "Scheduler", link: "/guides/scheduler" },
            { text: "Operator console (React/Tauri)", link: "/guides/react-tauri-ui" },
            { text: "Wire Langfuse + Prometheus", link: "/guides/observability" },
            { text: "Run multiple instances", link: "/guides/multi-instance" },
            { text: "Deploy via GHCR", link: "/guides/deploy" },
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
