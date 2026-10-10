import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Inline artifacts (ADR 0118 D2 / S7b): an `artifact-ref` with `inline: true` hosts the
// artifact's OWN embed frame (plugins/artifact/shell.js ?embed=<id>&v=<n>) right in the
// transcript instead of leaving a chip that opens the panel. The embed is the REAL plugin
// shell, so this drives the console host → embed frame → nested artifact frame end to end,
// and confirms history hydration renders the inline answer identically after a reload (without
// re-opening the panel, which is live-stream-only). The chip form is covered by
// artifact-chip.spec.ts; inert/off states are shared logic exercised there.

// A single-version html artifact, with a marker the nested artifact frame renders so we can
// prove the embed actually mounted the model's markup (not just the shell chrome).
const INLINE_STORE = {
  current: "art-inline",
  artifacts: [
    {
      id: "art-inline",
      kind: "html",
      title: "Inline calc",
      versions: [
        { code: '<!doctype html><html><body><div id="inline-mark">INLINE OK</div></body></html>', ts: 1, by: "agent" },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function withInlineArtifact(page: Page) {
  // Enable the Artifact plugin (its right-dock view) for this page only — the shared mock's
  // runtime status doesn't list it, so adding it here keeps other specs' rails untouched.
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: INLINE_STORE }));
  await page.route("**/api/plugins/artifact/refs?*", (route) => {
    const ids = (new URL(route.request().url()).searchParams.get("ids") || "").split(",");
    const artifacts: Record<string, unknown> = {};
    for (const a of INLINE_STORE.artifacts) {
      if (!ids.includes(a.id)) continue;
      artifacts[a.id] = { title: a.title, kind: a.kind, version_count: a.version_count, oldest: 1 };
    }
    return route.fulfill({ json: { artifacts } });
  });
}

test.beforeEach(async ({ page }) => {
  await withInlineArtifact(page);
});

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

/** The model's markup, inside the embed's nested artifact frame. */
function inlineMark(page: Page) {
  return page.getByTestId("artifact-inline-frame").contentFrame().frameLocator("#frame").locator("#inline-mark");
}

test("an inline html artifact renders in the transcript and survives a reload", async ({ page }) => {
  await send(page, "ARTIFACTREF INLINE please");

  // The inline host renders instead of the chip button, with Open-in-panel in its head.
  await expect(page.getByTestId("artifact-ref-inline")).toBeVisible();
  await expect(page.getByTestId("artifact-ref-chip")).toHaveCount(0);
  await expect(page.getByTestId("artifact-inline-open")).toBeVisible();

  // The embed frame points at this artifact + version, and mounts the model's html.
  const embed = page.getByTestId("artifact-inline-frame");
  await expect(embed).toBeVisible();
  await expect(embed).toHaveAttribute("src", /embed=art-inline/);
  await expect(embed).toHaveAttribute("src", /v=1/);
  await expect(inlineMark(page)).toHaveText("INLINE OK");

  // The panel is NOT auto-opened for an inline ref — the answer is already in view.
  await expect(page.locator('iframe[title="Artifact"]')).toHaveCount(0);

  // Reload: history hydration renders the inline answer the same way (and still never opens
  // the panel — auto-open is live-stream-only).
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("artifact-inline-frame")).toBeVisible();
  await expect(inlineMark(page)).toHaveText("INLINE OK");
  await page.waitForTimeout(400);
  await expect(page.locator('iframe[title="Artifact"]')).toHaveCount(0);
});

test("Open in panel from an inline host opens the Artifact panel on that version", async ({ page }) => {
  await send(page, "ARTIFACTREF INLINE please");
  await expect(page.getByTestId("artifact-inline-frame")).toBeVisible();
  await page.getByTestId("artifact-inline-open").click();
  const panel = page.locator('iframe[title="Artifact"]');
  await expect(panel).toBeVisible();
  await expect(panel.contentFrame().locator("#art")).toHaveValue("art-inline");
});
