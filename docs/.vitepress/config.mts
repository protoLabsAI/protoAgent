import { defineConfig } from "vitepress";
import navigation from "./navigation.json";
import { processSearchTerm } from "./search-terms.mjs";
import catalog from "../../plugins/docs/catalog.json";

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

  markdown: {
    config(md) {
      // VitePress handles img URLs, but raw video/source/track/download URLs
      // also need the deployment base. Keep generated media plain HTML so the
      // offline Markdown reader can serve the same block without Vue.
      const render = md.renderer.rules.html_block;
      md.renderer.rules.html_block = (tokens, idx, options, env, self) => {
        const html = render ? render(tokens, idx, options, env, self) : tokens[idx].content;
        return html.replace(
          /\b(src|poster|href)="\/media\/([a-zA-Z0-9_./-]+)"/g,
          (_, attr, path) => `:${attr}="'${base}media/${path}'"`,
        );
      };
    },
  },

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
      options: {
        // Search metadata changes the index only; every page and old URL stays readable.
        _render(src, env, md) {
          const path = env.relativePath;
          const page = ({ ...catalog.pages, ...catalog.website_pages } as Record<string, { title: string; status: string }>)[path];
          const status = page?.status ??
            (catalog.section_status as Record<string, string>)[path.split("/")[0]] ?? "current";
          if (status === "moved") return "";
          const label = ({ decision: "ADR", historical: "Historical", deprecated: "Deprecated" } as Record<string, string>)[status];
          const html = md.render(src, env);
          if (env.frontmatter?.search === false) return "";
          // Label the search result after rendering so its heading anchor still
          // matches the published page, including explicit legacy heading IDs.
          return label ? html.replace(/(<h1\b[^>]*>)/, "$1[" + label + "] ") : html;
        },
        miniSearch: {
          options: {
            processTerm: processSearchTerm,
          },
          searchOptions: {
            boost: { title: 6, titles: 4, text: 1 },
            // VitePress serializes this callback into the browser: keep it self-contained.
            boostDocument(_id, term, fields) {
              const labels = [fields?.title, ...(fields?.titles ?? [])].join(" ");
              if (/\[(ADR|Historical|Deprecated)\]/.test(labels) &&
                  !/^(adr|decision|history|historical|deprecated|\d{4})$/i.test(term)) {
                return labels.includes("[ADR]") ? 0.25 : 0.15;
              }
              return 1;
            },
          },
        },
      },
    },

    footer: {
      message: "Part of the protoLabs autonomous development studio.",
      copyright: "© 2026 protoLabs.studio",
    },
  },
});
