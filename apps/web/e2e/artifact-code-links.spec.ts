import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Code-linked mermaid diagrams, end to end in the console (ADR 0038 amendment): the agent's
// show_artifact (the MERMAID_LINKS scenario) opens the REAL Artifact panel on a sequence diagram
// whose messages carry stored code links; clicking one opens the code pane (ADR 0112) at that
// range with the link's note — the nested sandbox → shell → console → code pane chain. With the
// pane toolset off and no editor, the console says where the code is instead.

const SEQ = [
  "sequenceDiagram",
  "  participant C as Client",
  "  participant S as Server",
  "  C->>S: request",
  "  S->>S: authorize(req, token)",
  "  S-->>C: 401 or response",
].join("\n");

const STORE = {
  current: "art-links",
  artifacts: [
    {
      id: "art-links",
      kind: "mermaid",
      title: "authorize() flow",
      versions: [
        {
          code: SEQ,
          ts: 1,
          by: "agent",
          links: {
            "msg:2": { project: "app", path: "src/server.ts", line: 23, end_line: 29, note: "constant-time bearer check" },
            "participant:Server": { project: "app", path: "src/server.ts", line: 34, end_line: 34, note: "" },
          },
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function setup(page: Page, codePane: boolean) {
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({
      response: res,
      json: { ...body, code_pane: { enabled: codePane }, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] },
    });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: STORE }));
  await page.route("**/api/plugins/artifact/refs?*", (route) =>
    route.fulfill({
      json: { artifacts: { "art-links": { title: "authorize() flow", kind: "mermaid", version_count: 1, oldest: 1 } } },
    }),
  );
}

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

const diagram = (page: Page) => page.frameLocator('iframe[title="Artifact"]').frameLocator("#frame");

test("clicking a linked message opens the code pane at that range, with the note", async ({ page }) => {
  await setup(page, true);
  await send(page, "MERMAID_LINKS draw the auth flow");
  await expect(page.getByTestId("artifact-ref-chip")).toContainText("authorize() flow");
  const msg = diagram(page).locator('[data-lk="msg:2"]').first();
  await expect(msg).toBeVisible({ timeout: 20_000 });
  await msg.click();
  await expect(page.getByTestId("code-pane")).toBeVisible();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  await expect(page.getByTestId("code-pane-range")).toHaveText("L23–29");
  await expect(page.getByTestId("code-pane-note")).toContainText("constant-time bearer check");
  // Side by side: the pane took a dock that shows neither chat nor the diagram.
  await expect(page.locator('iframe[title="Artifact"]')).toBeVisible();
  // An unlinked participant opens nothing new: the pane stays on the message's range.
  await diagram(page).locator('rect.actor[name="C"]').first().click({ position: { x: 10, y: 10 } });
  await page.waitForTimeout(300);
  await expect(page.getByTestId("code-pane-range")).toHaveText("L23–29");
  // The participant link re-points it.
  await diagram(page).locator('[data-lk="participant:Server"]').first().click({ position: { x: 10, y: 10 } });
  await expect(page.getByTestId("code-pane-range")).toHaveText("L34");
});

test("with no code pane and no editor, the console tells the operator where the code is", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.addInitScript(() => localStorage.setItem("protoagent.editor", "off"));
  await setup(page, false);
  await send(page, "MERMAID_LINKS draw the auth flow");
  const msg = diagram(page).locator('[data-lk="msg:2"]').first();
  await expect(msg).toBeVisible({ timeout: 20_000 });
  await msg.click();
  await expect(page.getByText("app/src/server.ts:23-29", { exact: false })).toBeVisible();
  await expect(page.getByTestId("code-pane")).toHaveCount(0);
});
