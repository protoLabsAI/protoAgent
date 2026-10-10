import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Streamed inline-artifact preview (ADR 0118 D3 / S8c): while a `show_artifact` call with inline
// placement is still writing, the console decodes its `code` arg into a live buffer (S3) and the
// WorkBlock hosts a sandboxed PREVIEW of the half-written markup — so the operator isn't left on a
// spinner until the tool ends. When the tool finishes and the artifact-ref lands, the preview gives
// way to the artifact's own inline frame (S7b).
//
// The turn is parked mid-tool by the mock ("PARK THE TOOL"), so the preview is guaranteed to be on
// screen BEFORE the tool ends — no race against machine speed. The inline-frame handover and the
// chip/inert states are covered by artifact-inline.spec.ts; this spec owns the live-preview path.

// A single-version html artifact the handover frame resolves to (same shape as artifact-inline).
const INLINE_STORE = {
  current: "art-inline",
  artifacts: [
    {
      id: "art-inline",
      kind: "html",
      title: "Streamed page",
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
  // Enable the Artifact plugin (its right-dock view) for this page only — so the artifact-ref that
  // lands at the end of the turn resolves and hosts its inline frame.
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

test("the inline artifact streams a live preview before the tool ends, then hands over to the real frame", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  // The session id rides the stream request (A2A contextId); the park is keyed by it, so this spec
  // can only ever release its own turn on the shared mock.
  const streamRequest = page.waitForRequest(
    (r) => r.url().endsWith("/a2a") && r.method() === "POST" && r.postDataJSON()?.method === "SendStreamingMessage",
  );
  await composer.fill("ARTIFACTREF STREAM PARK THE TOOL");
  await composer.press("Enter");
  const sessionId = String((await streamRequest).postDataJSON().params.message.contextId);

  // While the tool is still running (parked), the WorkBlock hosts the live preview: the card and
  // its sandboxed preview frame are on screen …
  const preview = page.locator('[data-testid="streaming-preview"]');
  await expect(preview).toBeVisible();
  await expect(preview.locator('[data-testid="streaming-preview-frame"]')).toBeVisible();
  // … and the turn has NOT ended yet (the answer only arrives after release).
  await expect(page.getByText("Here's the streamed page.")).toHaveCount(0);

  // Release the turn: tool end → artifact-ref → answer → terminal frame.
  const release = await page.request.post(`/api/__test__/turns/${encodeURIComponent(sessionId)}/release`);
  expect((await release.json()).released).toBe(true);

  // Handover: the streamed preview gives way to the artifact's OWN inline frame, and the answer
  // lands below it.
  await expect(page.getByText("Here's the streamed page.")).toBeVisible();
  await expect(page.locator('[data-testid="artifact-ref-inline"]')).toBeVisible();
  await expect(preview).toHaveCount(0);
});
