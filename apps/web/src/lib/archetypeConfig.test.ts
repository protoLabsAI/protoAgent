import { describe, it, expect } from "vitest";

import {
  archetypeConfigFields,
  configMissingRequired,
  fieldId,
  hasConfigFields,
  mcpItemLabel,
  previewMcpSummary,
  previewSecretsSummary,
  splitConfigValues,
} from "./archetypeConfig";
import type { ArchetypePreview } from "./types";

// The new-agent Configure step (#2041 slice 3) and the enriched preview dialog both derive
// their behavior from these pure helpers, so they carry the real coverage: the form spec a
// bundle preview flattens to, the gate on required inputs, the split back into the two
// create() channels, and the read-only preview summaries.

// A bundle preview with one MCP server (a plain root input + a secret token) and one
// declared standalone secret — the GitHub-style case from the acceptance criteria.
function githubPreview(): ArchetypePreview {
  return {
    id: "product-stack",
    bundle: {
      kind: "bundle",
      name: "Product stack",
      members: [],
      mcp: [
        {
          id: "github",
          name: "GitHub",
          requires: "token",
          template: { env: { GITHUB_TOKEN: "${github_token}" }, args: ["--root", "${root}"] },
          inputs: [
            { key: "root", label: "Repo root", placeholder: "/work", required: true },
            { key: "github_token", label: "GitHub token", secret: true, required: true },
          ],
        },
      ],
      secrets: [{ key: "BRAVE_API_KEY", label: "Brave API key", secret: true }],
    },
  };
}

describe("archetypeConfigFields — flatten a bundle preview into form fields", () => {
  it("emits MCP inputs first, then declared secrets, tagged by origin", () => {
    const fields = archetypeConfigFields(githubPreview());
    expect(fields.map((f) => [f.origin, f.key])).toEqual([
      ["input", "root"],
      ["input", "github_token"],
      ["secret", "BRAVE_API_KEY"],
    ]);
  });

  it("masks secret MCP inputs and always masks declared secrets; plain inputs stay unmasked", () => {
    const fields = archetypeConfigFields(githubPreview());
    expect(fields.find((f) => f.key === "root")?.secret).toBe(false);
    expect(fields.find((f) => f.key === "github_token")?.secret).toBe(true);
    expect(fields.find((f) => f.key === "BRAVE_API_KEY")?.secret).toBe(true);
  });

  it("returns no fields for a code-free archetype (bundle: null) — backward compat", () => {
    const empty: ArchetypePreview = { id: "basic", bundle: null };
    expect(archetypeConfigFields(empty)).toEqual([]);
    expect(hasConfigFields(empty)).toBe(false);
    expect(archetypeConfigFields(undefined)).toEqual([]);
  });

  it("returns no fields for a bundle that declares neither mcp inputs nor secrets", () => {
    const bare: ArchetypePreview = { id: "x", bundle: { kind: "bundle", members: [] } };
    expect(hasConfigFields(bare)).toBe(false);
  });
});

describe("configMissingRequired — required-input gate", () => {
  const fields = archetypeConfigFields(githubPreview());

  it("is true while any required field is blank or whitespace", () => {
    expect(configMissingRequired(fields, {})).toBe(true);
    expect(
      configMissingRequired(fields, { [fieldId({ origin: "input", key: "root" })]: "   " }),
    ).toBe(true);
  });

  it("is false once every required field has a value", () => {
    const values = {
      [fieldId({ origin: "input", key: "root" })]: "/work",
      [fieldId({ origin: "input", key: "github_token" })]: "ghp_1",
    };
    // BRAVE_API_KEY is not required, so leaving it blank is fine.
    expect(configMissingRequired(fields, values)).toBe(false);
  });
});

describe("splitConfigValues — collected form values back into create() channels", () => {
  const fields = archetypeConfigFields(githubPreview());

  it("routes inputs to the map and declared secrets to the list, dropping blanks", () => {
    const values = {
      [fieldId({ origin: "input", key: "root" })]: "/work",
      [fieldId({ origin: "input", key: "github_token" })]: " ghp_1 ",
      [fieldId({ origin: "secret", key: "BRAVE_API_KEY" })]: "",
    };
    expect(splitConfigValues(fields, values)).toEqual({
      inputs: { root: "/work", github_token: "ghp_1" },
      secrets: [],
    });
  });

  it("carries a filled declared secret into the secrets list", () => {
    const values = { [fieldId({ origin: "secret", key: "BRAVE_API_KEY" })]: "brv_9" };
    expect(splitConfigValues(fields, values)).toEqual({
      inputs: {},
      secrets: [{ key: "BRAVE_API_KEY", value: "brv_9" }],
    });
  });

  it("keeps an MCP input and a declared secret sharing a key from colliding", () => {
    const preview: ArchetypePreview = {
      id: "clash",
      bundle: {
        kind: "bundle",
        members: [],
        mcp: [{ id: "s", name: "S", template: {}, inputs: [{ key: "TOKEN", label: "Input token" }] }],
        secrets: [{ key: "TOKEN", label: "Secret token", secret: true }],
      },
    };
    const clashFields = archetypeConfigFields(preview);
    const values = {
      [fieldId({ origin: "input", key: "TOKEN" })]: "from-input",
      [fieldId({ origin: "secret", key: "TOKEN" })]: "from-secret",
    };
    expect(splitConfigValues(clashFields, values)).toEqual({
      inputs: { TOKEN: "from-input" },
      secrets: [{ key: "TOKEN", value: "from-secret" }],
    });
  });
});

describe("preview summaries — read-only display in ArchetypePreviewDialog", () => {
  it("annotates each MCP server with what it needs", () => {
    expect(mcpItemLabel({ id: "g", name: "GitHub", requires: "token", template: {} })).toBe(
      "GitHub (needs token)",
    );
    expect(mcpItemLabel({ id: "n", name: "Notion", template: {} })).toBe("Notion");
    expect(
      previewMcpSummary([
        { id: "g", name: "GitHub", requires: "token", template: {} },
        { id: "b", name: "Brave Search", requires: "API key", template: {} },
      ]),
    ).toBe("GitHub (needs token), Brave Search (needs API key)");
  });

  it("lists secret labels", () => {
    expect(
      previewSecretsSummary([
        { key: "GH", label: "GitHub token" },
        { key: "BR", label: "Brave API key" },
      ]),
    ).toBe("GitHub token, Brave API key");
    expect(previewSecretsSummary(undefined)).toBe("");
  });
});
