import { describe, expect, it } from "vitest";

// Regression guard for #3686 (part 2): part 1 tore out the dead shadcn/Tailwind CSS bridge
// (app/tailwind.css + tailwind.config.cjs + the main.tsx import); this part removes the npm
// deps that only that bridge and its abandoned shadcn scaffold pulled in, plus the shadcn
// config file (components.json). A whole-repo grep found no imports of any of them. This sweep
// keeps the manifest from regrowing them.
//
// Reads the manifests as `?raw` text via a compile-time glob rather than node:fs — this tsconfig
// has no node types and under jsdom `import.meta.url` is an http: URL, so filesystem access is a
// trap (mirrors tailwindBridgeRemoved.test.ts). The glob is rooted one dir up (apps/web/), so a
// deleted json file simply drops out of the result — that is how we assert components.json is gone.
const ROOT_JSON = import.meta.glob("../*.json", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const PACKAGE_JSON = ROOT_JSON["../package.json"];
const manifest = JSON.parse(PACKAGE_JSON) as {
  dependencies: Record<string, string>;
  devDependencies: Record<string, string>;
};

// Deps that only the retired bridge / shadcn scaffold used. tailwindcss is the devDep half.
const REMOVED_DEPS = [
  "@radix-ui/react-dropdown-menu",
  "class-variance-authority",
  "clsx",
  "react-markdown",
  "rehype-highlight",
  "remark-gfm",
  "tailwind-merge",
  "tailwindcss-animate",
  "tailwindcss",
];

// The markdown/code-block + graph stack that is genuinely wired in and must survive the prune.
const KEPT_DEPS = [
  "streamdown",
  "katex",
  "@streamdown/code",
  "shiki",
  "@shikijs/themes",
  "@shikijs/transformers",
  "@xyflow/react",
  "@protolabsai/design",
  "@protolabsai/ui",
  "lucide-react",
];

describe("shadcn/Tailwind npm deps stay removed (#3686 part 2)", () => {
  it("package.json is present in the sweep (glob actually resolved it)", () => {
    expect(typeof PACKAGE_JSON).toBe("string");
    expect(PACKAGE_JSON.length).toBeGreaterThan(0);
  });

  it("components.json (the shadcn config) is gone from the tree", () => {
    const shadcnConfig = Object.keys(ROOT_JSON).filter((k) => /components\.json$/.test(k));
    expect(shadcnConfig).toEqual([]);
  });

  it("no removed dep is listed under dependencies or devDependencies", () => {
    const all = { ...manifest.dependencies, ...manifest.devDependencies };
    const stillPresent = REMOVED_DEPS.filter((dep) => dep in all);
    expect(stillPresent).toEqual([]);
  });

  it("the in-use markdown/code-block/graph deps survive the prune", () => {
    const missing = KEPT_DEPS.filter((dep) => !(dep in manifest.dependencies));
    expect(missing).toEqual([]);
  });

  it("autoprefixer + postcss stay (part 1 kept an autoprefixer-only postcss.config.cjs)", () => {
    // Vite bundles its own postcss, but the console still ships an autoprefixer-only
    // postcss.config.cjs from part 1, so both devDeps remain load-bearing.
    expect(manifest.devDependencies).toHaveProperty("autoprefixer");
    expect(manifest.devDependencies).toHaveProperty("postcss");
  });

  it("the manifest text is real, not a stubbed empty import", () => {
    // A `?raw` json import that vite stubbed to "" would make JSON.parse throw before the
    // assertions run; guard the length explicitly so the failure names the cause.
    expect(PACKAGE_JSON.length).toBeGreaterThan(100);
  });
});
