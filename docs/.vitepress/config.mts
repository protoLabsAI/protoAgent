import { defineConfig } from "vitepress";
import navigation from "./navigation.json";

// GitHub Pages serves under /protoAgent/; the Cloudflare marketing build folds
// the docs in at /docs/ (DOCS_BASE=/docs/). Env-driven so both coexist.
const base = process.env.DOCS_BASE || "/protoAgent/";

// Social-card copy. Mirrors the marketing site's BaseLayout so a shared docs
// link unfurls with the same card as a shared homepage link.
const SOCIAL_DESCRIPTION =
  "AI agents on your own machine. Chat, work with documents, connect tools, and hand coding jobs to your CLI agents.";
const SOCIAL_IMAGE = "https://agent.protolabs.studio/docs/social-preview.png";

export default defineConfig({
  title: "protoAgent",
  description:
    "Set up, use, extend, and operate local AI agents with protoAgent.",
  base,

  // Follow the reader's OS colour scheme, same as the marketing site this sits
  // under at agent.protolabs.studio — and give them VitePress's toggle on top.
  // This was "force-dark", which the theme now documents as legacy ("locks to
  // dark, disables light mode"): /docs stayed black for light-mode readers while
  // the marketing pages one path segment up adapted.
  appearance: "auto",

  // Tutorials legitimately reference the local dev server (http://localhost:7870);
  // VitePress treats dead links as fatal, so skip just the localhost ones.
  ignoreDeadLinks: "localhostLinks",

  // docs/dev/ is the team's internal engineering area (handoffs + notes) — it
  // lives in the repo (committed, shared) but is NOT part of the published site.
  srcExclude: ["dev/**", "design/**"],

  head: [
    ["link", { rel: "icon", type: "image/svg+xml", href: `${base}favicon.svg` }],
    // Social cards — the same 1280×640 card GitHub serves as the repo's social
    // preview (docs/public/social-preview.png), as a canonical absolute URL so
    // it resolves regardless of which base the build serves under.
    ["meta", { property: "og:type", content: "website" }],
    ["meta", { property: "og:title", content: "protoAgent" }],
    ["meta", { property: "og:description", content: SOCIAL_DESCRIPTION }],
    ["meta", { property: "og:image", content: SOCIAL_IMAGE }],
    ["meta", { property: "og:image:width", content: "1280" }],
    ["meta", { property: "og:image:height", content: "640" }],
    ["meta", { name: "twitter:card", content: "summary_large_image" }],
    ["meta", { name: "twitter:title", content: "protoAgent" }],
    ["meta", { name: "twitter:description", content: SOCIAL_DESCRIPTION }],
    ["meta", { name: "twitter:image", content: SOCIAL_IMAGE }],
  ],

  themeConfig: {
    logo: "/favicon.svg",

    nav: navigation.nav,
    sidebar: Object.fromEntries(
      // VitePress sorts by slash depth, so equal-depth page keys must precede
      // section fallbacks (and longer page prefixes must precede shorter ones).
      navigation.sidebars
        .flatMap(({ routes, groups }) => routes.map((route) => [route, groups] as const))
        .sort(([a], [b]) => b.length - a.length),
    ),

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
