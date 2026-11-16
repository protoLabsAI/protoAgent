import { expect, test } from "@playwright/test";
import { seedCurrentChat } from "./chat-helpers";

const tap = "artifact-open";
test.use({ extraHTTPHeaders: { "x-e2e-bus-tap": tap } });

test("an artifact open event opens a new browser tab from the active chat", async ({ page, context }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const sent = page.waitForRequest((request) => request.url().endsWith("/a2a") && request.method() === "POST");
  await seedCurrentChat(page);
  const session = String((await sent).postDataJSON().params.message.contextId);
  const path = `/plugins/artifact/media/artifact/a-123/blob?version=1&exp=1900000000&t=${"a".repeat(64)}`;
  await context.route(`**${path.split("?")[0]}**`, (route) => route.fulfill({ contentType: "text/plain", body: "artifact bytes" }));
  const opened = context.waitForEvent("page");
  const result = await page.request.post("/api/__test__/bus", {
    data: { tap, frames: [{ topic: "artifact.open_url", data: { session_id: session, path } }] },
  });
  expect((await result.json()).streams).toBeGreaterThan(0);
  const tab = await opened;
  await expect(tab).toHaveURL(new RegExp("/plugins/artifact/media/artifact/a-123/blob"));
  await expect(tab.locator("body")).toHaveText("artifact bytes");
  expect(tab.url()).not.toContain("token=");
  expect(await tab.evaluate(() => window.opener)).toBeNull();
});

// #4939: a RENDERED artifact's open_artifact link is the standalone `/page`, not `/blob` — the
// watcher used to accept only `/blob`, so the agent's tab silently never opened for html/react/etc.
test("an open event for a rendered artifact's standalone page opens it too", async ({ page, context }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const sent = page.waitForRequest((request) => request.url().endsWith("/a2a") && request.method() === "POST");
  await seedCurrentChat(page);
  const session = String((await sent).postDataJSON().params.message.contextId);
  const path = `/plugins/artifact/media/artifact/a-456/page?version=2&exp=1900000000&t=${"c".repeat(64)}`;
  await context.route(`**${path.split("?")[0]}**`, (route) => route.fulfill({ contentType: "text/html", body: "<p id=page>mockup</p>" }));
  const opened = context.waitForEvent("page");
  const result = await page.request.post("/api/__test__/bus", {
    data: { tap, frames: [{ topic: "artifact.open_url", data: { session_id: session, path } }] },
  });
  expect((await result.json()).streams).toBeGreaterThan(0);
  const tab = await opened;
  await expect(tab).toHaveURL(new RegExp("/plugins/artifact/media/artifact/a-456/page\\?version=2"));
  await expect(tab.locator("#page")).toHaveText("mockup");
  expect(await tab.evaluate(() => window.opener)).toBeNull();
});
