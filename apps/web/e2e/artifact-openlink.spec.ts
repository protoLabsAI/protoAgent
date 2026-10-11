import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// openLink bridge (ADR 0118 D4, #4076 F1): an inline artifact's `window.protoArtifact.openLink(url)`
// is a PROPOSAL behind the same host confirm chip as send — "Open ‹host› in a new tab?" with Open /
// Dismiss / "Always allow links". The tab opens only from the chip's click, link trust is separate
// from send trust, and the frame's Promise resolves only when the tab really opened. Drives the
// real stack: nested artifact frame → embed shell (plugins/artifact/shell.js) → console host.

const LINK = "https://docs.example.com/guide";

const LINK_BUTTON_CODE =
  "<!doctype html><html><body>" +
  '<button id="link-btn">Read the guide</button>' +
  '<div id="link-status"></div>' +
  "<script>" +
  "document.getElementById('link-btn').addEventListener('click',function(){" +
  `window.protoArtifact.openLink('${LINK}')` +
  ".then(function(){document.getElementById('link-status').textContent='opened';})" +
  ".catch(function(e){document.getElementById('link-status').textContent='rejected: '+(e&&e.message||e);});" +
  "});" +
  "</script>" +
  "</body></html>";

const STORE = {
  current: "art-inline",
  artifacts: [
    {
      id: "art-inline",
      kind: "html",
      title: "Inline calc",
      versions: [{ code: LINK_BUTTON_CODE, ts: 1, by: "agent" }],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function openInlineLinkArtifact(page: Page) {
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: STORE }));
  await page.route("**/api/plugins/artifact/refs?*", (route) =>
    route.fulfill({
      json: { artifacts: { "art-inline": { title: "Inline calc", kind: "html", version_count: 1, oldest: 1 } } },
    }),
  );
  // The destination: never leave the sandbox for the real internet.
  await page.context().route("https://docs.example.com/**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<title>guide</title>the guide" }),
  );

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("ARTIFACTREF INLINE please");
  await composer.press("Enter");
  await expect(page.getByTestId("artifact-ref-inline")).toBeVisible();
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
  await expect(linkButton(page)).toBeVisible();
  // Click-to-activate (#4154), then the artifact's own button.
  await page.getByTestId("artifact-ref-inline").getByTestId("frame-guard-shield").click();
  await expect(page.getByTestId("artifact-ref-inline").getByTestId("frame-guard-shield")).toHaveCount(0);
}

function nested(page: Page) {
  return page.getByTestId("artifact-inline-frame").contentFrame().frameLocator("#frame");
}
const linkButton = (page: Page) => nested(page).locator("#link-btn");
const linkStatus = (page: Page) => nested(page).locator("#link-status");

test("openLink asks first; Open opens the tab and only then resolves the frame's promise", async ({ page }) => {
  await openInlineLinkArtifact(page);
  let popups = 0;
  page.context().on("page", () => popups++);

  await linkButton(page).click();
  const chip = page.getByTestId("artifact-open-confirm");
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("Open docs.example.com in a new tab?");
  await expect(page.getByTestId("artifact-open-confirm-text")).toHaveText(LINK);
  await expect(chip).toContainText("Always allow links from this artifact");
  // Nothing opened and the frame hasn't heard anything yet.
  await page.waitForTimeout(300);
  expect(popups).toBe(0);
  await expect(linkStatus(page)).toHaveText("");

  const [tab] = await Promise.all([page.context().waitForEvent("page"), page.getByTestId("artifact-open-confirm-ok").click()]);
  await tab.waitForURL(LINK);
  // noopener + no Referer: the new tab can't reach the console.
  expect(await tab.evaluate(() => window.opener)).toBeNull();
  expect(await tab.evaluate(() => document.referrer)).toBe("");
  await expect(linkStatus(page)).toHaveText("opened");
  await expect(chip).toHaveCount(0);
});

test("Dismiss rejects openLink and opens nothing", async ({ page }) => {
  await openInlineLinkArtifact(page);
  let popups = 0;
  page.context().on("page", () => popups++);
  await linkButton(page).click();
  await page.getByTestId("artifact-open-confirm-dismiss").click();
  await expect(linkStatus(page)).toContainText(/rejected: .*dismissed/i);
  await expect(page.getByTestId("artifact-open-confirm")).toHaveCount(0);
  expect(popups).toBe(0);
});

test("a blocked pop-up rejects openLink instead of reporting success", async ({ page }) => {
  // Stand in for the browser's pop-up blocker on the console page: window.open returns null.
  await page.addInitScript(() => {
    if (window.top === window) window.open = () => null;
  });
  await openInlineLinkArtifact(page);
  await linkButton(page).click();
  await page.getByTestId("artifact-open-confirm-ok").click();
  await expect(linkStatus(page)).toContainText(/rejected: .*blocked/i);
  await expect(page.getByTestId("artifact-send-rejected")).toContainText(/blocked/i);
});

test("trusting an artifact's sends does not trust its links", async ({ page }) => {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("protoagent:frame-send-trust:v1", JSON.stringify(["artifact:art-inline"]));
    } catch {
      /* ignore */
    }
  });
  await openInlineLinkArtifact(page);
  await linkButton(page).click();
  await expect(page.getByTestId("artifact-open-confirm")).toBeVisible();
});
