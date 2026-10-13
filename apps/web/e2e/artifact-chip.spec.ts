import { expect, test, type Page } from "@playwright/test";

import { withArtifactPanel } from "./artifactPanel";

// The artifact-ref chip (#3617): an artifact create/revise leaves a chip in the transcript
// that opens the Artifact panel on EXACTLY that artifact + version — the code-ref chip's
// sibling. The live turn auto-opens the panel (desktop); history hydration never does. The
// panel is the REAL plugin shell (plugins/artifact/shell.js), so these drive the console →
// iframe `protoArtifact:select` deep-link end to end. Mobile: mobile.spec.ts.

test.beforeEach(async ({ page }) => {
  await withArtifactPanel(page);
});

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

const panel = (page: Page) => page.frameLocator('iframe[title="Artifact"]');
const panelFrame = (page: Page) => page.locator('iframe[title="Artifact"]');

/** The version label + the selected artifact + the rendered svg's id, inside the panel. */
async function expectPanelOn(page: Page, label: string, svgId: string) {
  const p = panel(page);
  await expect(p.locator("#vlabel")).toHaveText(label);
  await expect(p.locator("#art")).toHaveValue("art-chain");
  await expect(p.frameLocator("#frame").locator(`svg#${svgId}`)).toHaveCount(1);
}

test("live: the chip renders and the panel opens on the version the agent wrote", async ({ page }) => {
  await send(page, "ARTIFACTREF V3 make it blue");
  const chip = page.getByTestId("artifact-ref-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("Signal chart");
  await expect(chip).toContainText("v3");
  await expect(chip).not.toContainText("of 3");
  await expect(chip).toContainText("svg");
  // Auto-opened on the right dock (not chat's), on v3.
  await expect(page.locator(".pl-appshell__col--right").locator('iframe[title="Artifact"]')).toBeVisible();
  await expectPanelOn(page, "v3 of 3", "v3");
});

test("an older-version chip opens THAT version, reads 'v1 of 3', and pauses follow-newest", async ({ page }) => {
  await send(page, "ARTIFACTREF V1 go back");
  const chip = page.getByTestId("artifact-ref-chip");
  await expect(chip).toContainText("v1 of 3");
  await expect(chip).toHaveAttribute("data-older", "true");
  await expectPanelOn(page, "v1 of 3", "v1");
  // The shell persisted the pin: auto-follow off, so the next agent edit won't yank it away.
  const sel = await panelFrame(page)
    .contentFrame()
    .locator("body")
    .evaluate(() => JSON.parse(localStorage.getItem("protoartifact.sel") || "null"));
  expect(sel).toEqual({ selId: "art-chain", selVer: 0, followNewest: false });
});

test("a reload does NOT re-open the panel (live stream only); the chip still opens it", async ({ page }) => {
  await send(page, "ARTIFACTREF V2 once");
  await expect(panelFrame(page)).toBeVisible();
  await expectPanelOn(page, "v2 of 3", "v2");
  // Move the right dock back to Work, reload: the hydrated chip must not steal it again.
  await page.getByRole("button", { name: "Work", exact: true }).first().click();
  await expect(panelFrame(page)).toHaveCount(0);
  await page.reload({ waitUntil: "load" });
  const chip = page.getByTestId("artifact-ref-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("v2 of 3");
  await page.waitForTimeout(600);
  await expect(panelFrame(page)).toHaveCount(0);
  // A click opens a COLLAPSED/unmounted panel straight onto v2 (the select waits for the
  // page's ready ping, then lands).
  await chip.click();
  await expectPanelOn(page, "v2 of 3", "v2");
});

test("a chip click re-points a panel that is already open on another version", async ({ page }) => {
  await send(page, "ARTIFACTREF V3 first");
  await expectPanelOn(page, "v3 of 3", "v3");
  // Step the panel away with its own controls, then click the chip: back to v3.
  await panel(page).locator("#vprev").click();
  await expect(panel(page).locator("#vlabel")).toHaveText("v2 of 3");
  await page.getByTestId("artifact-ref-chip").click();
  await expectPanelOn(page, "v3 of 3", "v3");
});

test("a deleted artifact's chip renders inert — nothing to click", async ({ page }) => {
  await send(page, "ARTIFACTREF GONE whatever");
  const inert = page.getByTestId("artifact-ref-gone");
  await expect(inert).toBeVisible();
  await expect(inert).toContainText("no longer available");
  await expect(page.getByTestId("artifact-ref-chip")).toHaveCount(0);
});

test("with the Artifact plugin off the chip renders inert and nothing opens", async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await send(page, "ARTIFACTREF V3 off");
  const off = page.getByTestId("artifact-ref-off");
  await expect(off).toBeVisible();
  await expect(off).toContainText("the Artifact panel is off");
  await page.waitForTimeout(400);
  await expect(panelFrame(page)).toHaveCount(0);
});

test("the shell ignores a select posted by the artifact's own (model-authored) frame", async ({ page }) => {
  await send(page, "ARTIFACTREF V3 guard");
  await expectPanelOn(page, "v3 of 3", "v3");
  const shell = await panelFrame(page).contentFrame().locator("body").elementHandle();
  const shellFrame = await shell!.ownerFrame();
  const nested = await (await shellFrame!.locator("#frame").elementHandle())!.contentFrame();
  // Generated code runs in the nested sandbox and can reach `parent` — the shell. It must not
  // be able to drive the panel's selection (only the embedding console may).
  await nested!.evaluate(() => {
    parent.postMessage({ type: "protoArtifact:select", id: "art-chain", ver: 1 }, "*");
    // …nor by forging the console's message from a sibling path: the top window is not the
    // shell's parent either.
    top!.postMessage({ type: "protoArtifact:select", id: "art-chain", ver: 1 }, "*");
  });
  await page.waitForTimeout(500);
  await expect(panel(page).locator("#vlabel")).toHaveText("v3 of 3");
});

test("a chip opens a freshly mounted panel straight onto its artifact, not the remembered one", async ({ page }) => {
  // #4076: the panel remembered ANOTHER artifact (protoartifact.sel), so boot rendered that one and
  // the chip's select re-targeted the frame ~2ms later — mid-navigation. Chrome drops a srcdoc
  // swapped in that window, which left the panel stale or black under the right picker label until
  // a reload. The deep-link now resolves before the first render: ONE srcdoc, the asked-for one.
  await page.addInitScript(() => {
    if (!location.pathname.endsWith("/plugins/artifact/view") || location.search) return;
    try {
      localStorage.setItem("protoartifact.sel", JSON.stringify({ selId: "art-other", selVer: null, followNewest: false }));
    } catch {
      /* storage blocked — the spec then can't seed, and the count below still holds */
    }
    const w = window as unknown as { __srcdocs: string[] };
    w.__srcdocs = [];
    const d = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, "srcdoc")!;
    Object.defineProperty(HTMLIFrameElement.prototype, "srcdoc", {
      configurable: true,
      get: d.get,
      set(v: string) {
        w.__srcdocs.push(/<svg id="(\w+)"/.exec(v)?.[1] ?? "?");
        d.set!.call(this, v);
      },
    });
  });
  await send(page, "ARTIFACTREF V2 fresh mount");
  await expectPanelOn(page, "v2 of 3", "v2");
  const srcdocs = await panelFrame(page)
    .contentFrame()
    .locator("body")
    .evaluate(() => (window as unknown as { __srcdocs: string[] }).__srcdocs);
  expect(srcdocs).toEqual(["v2"]);
});
