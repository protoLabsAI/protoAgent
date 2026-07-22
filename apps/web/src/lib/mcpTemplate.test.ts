import { describe, it, expect } from "vitest";

import { fillTemplate } from "./mcpTemplate";

// fillTemplate is the shared `${input}` substitution (#2041 slice 3), extracted from
// McpCatalogDialog so the MCP catalog quick-add and the new-agent Configure step share one
// implementation. These guard its recursive substitution across the template shapes MCP
// entries actually use (args arrays, nested env/headers maps).

describe("fillTemplate — ${key} substitution across a template", () => {
  it("substitutes placeholders in top-level strings", () => {
    expect(fillTemplate({ url: "https://api/${token}" }, { token: "abc" })).toEqual({
      url: "https://api/abc",
    });
  });

  it("recurses into arrays (an args list) and nested objects (env/headers)", () => {
    const template = {
      command: "npx",
      args: ["-y", "server", "--dir", "${root}"],
      env: { GITHUB_TOKEN: "${token}" },
      headers: { Authorization: "Bearer ${token}" },
    };
    expect(fillTemplate(template, { root: "/work", token: "ghp_1" })).toEqual({
      command: "npx",
      args: ["-y", "server", "--dir", "/work"],
      env: { GITHUB_TOKEN: "ghp_1" },
      headers: { Authorization: "Bearer ghp_1" },
    });
  });

  it("replaces an unfilled placeholder with an empty string (never leaves ${...})", () => {
    expect(fillTemplate({ env: { KEY: "${missing}" } }, {})).toEqual({ env: { KEY: "" } });
  });

  it("leaves non-string leaves (numbers, booleans, null) untouched", () => {
    const template = { port: 8080, tls: true, extra: null, name: "srv-${id}" };
    expect(fillTemplate(template, { id: "x" })).toEqual({
      port: 8080,
      tls: true,
      extra: null,
      name: "srv-x",
    });
  });
});
