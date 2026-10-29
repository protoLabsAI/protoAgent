import { devices, expect, test, type Page } from "@playwright/test";

import { withArtifactPanel } from "./artifactPanel";
import { withCodePane } from "./codePane";
import { STARTER_PROMPTS } from "../src/chat/starterPrompts";

// Tablet band of the desktop shell (#4336 S5). Runs under the `tablet` Playwright project
// (playwright.config.ts) with real device profiles — isMobile + hasTouch + DPR, so
// `(hover: none)` / `(pointer: coarse)` match the way they do on the hardware. Engine is forced
// to chromium for the same reason as the mobile project: CI installs chromium only, and these
// assert COMPUTED geometry, which every engine computes the same.
//
// What a headless engine can't show (keyboard geometry, safe areas, Safari's chrome) still
// needs a pass on a real tablet — see the PR's test steps.

type Profile = { name: string; portrait: boolean };

// Galaxy Tab S4 portrait is 712px wide — below the 768px shell switch, so it gets the phone
// shell (mobile.spec.ts territory) and is deliberately absent here.
const PROFILES: Profile[] = [
  { name: "iPad Mini", portrait: true },
  { name: "iPad Mini landscape", portrait: false },
  { name: "iPad Pro 11", portrait: true },
  { name: "iPad Pro 11 landscape", portrait: false },
  { name: "Galaxy Tab S4 landscape", portrait: false },
];

function profile(name: string) {
  // `defaultBrowserType` can't be set per describe block; the project pins chromium.
  const { defaultBrowserType, ...rest } = devices[name];
  return rest;
}

async function sendMessage(page: Page, text: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(text);
  await composer.press("Enter");
}

const leftCol = (page: Page) => page.locator(".pl-appshell__col--left");
const rightCol = (page: Page) => page.locator(".pl-appshell__col--right");

async function width(page: Page, sel: string): Promise<number> {
  return (await page.locator(sel).first().boundingBox())!.width;
}

/** Controls under the touch floor. 44px HIG minimum (hit area may be an ::after overlay);
 *  the resize handle is held to 24px (WCAG 2.5.8) — a 44px strip would straddle the chat
 *  column's edge and swallow taps near the send button (tablet.css). */
async function undersizedTargets(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll("button,a,[role=button],[role=tab],[role=separator],select")) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const after = getComputedStyle(el, "::after");
      const w = Math.max(r.width, parseFloat(after.minWidth) || 0, parseFloat(after.width) || 0);
      const h = Math.max(r.height, parseFloat(after.minHeight) || 0);
      const isHandle = el.classList.contains("pl-appshell__handle");
      const [minW, minH] = isHandle ? [24, 44] : [44, 44];
      if (w < minW || h < minH) {
        const label = el.getAttribute("aria-label") || el.className?.toString() || el.tagName;
        out.push(`${label} ${Math.round(w)}x${Math.round(h)}`);
      }
    }
    return out;
  });
}

for (const p of PROFILES) {
  test.describe(p.name, () => {
    test.use(profile(p.name));

    test("desktop shell, no horizontal scroll, roomy chat, touch-sized targets", async ({ page }) => {
      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator(".pl-rail").first()).toBeVisible();
      await expect(page.locator(".mshell")).toHaveCount(0); // tablets keep the desktop shell

      const { scrollW, vw } = await page.evaluate(() => ({
        scrollW: document.documentElement.scrollWidth,
        vw: window.innerWidth,
      }));
      expect(scrollW).toBeLessThanOrEqual(vw);

      // Before S5 portrait chat was 275px (iPad Mini) — narrower than a phone.
      expect(await width(page, ".chat-stage")).toBeGreaterThanOrEqual(500);

      // iOS zooms on focus below 16px.
      const zoomers = await page.evaluate(() =>
        [...document.querySelectorAll("input,textarea,select")]
          .filter((el) => el.getBoundingClientRect().width)
          .filter((el) => parseFloat(getComputedStyle(el).fontSize) < 16)
          .map((el) => el.className?.toString() || el.tagName),
      );
      expect(zoomers).toEqual([]);

      expect(await undersizedTargets(page)).toEqual([]);
    });

    test("starter chips have touch-sized boxes and fill the composer without sending", async ({ page }) => {
      await page.goto("/app/", { waitUntil: "load" });
      const composer = page.getByPlaceholder(/Message protoAgent/i);
      for (const { label, prompt, note } of STARTER_PROMPTS) {
        const chip = page.getByRole("button", { name: note ? `${label} ${note}` : label, exact: true });
        await expect(chip).toBeVisible();
        const box = (await chip.boundingBox())!;
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
        await chip.tap();
        await expect(composer).toHaveValue(prompt);
        await expect(page.locator(".pl-message--user")).toHaveCount(0);
      }
    });

    if (p.portrait) {
      test("portrait: the right panel floats over chat and taps away", async ({ page }) => {
        await page.goto("/app/", { waitUntil: "load" });
        await expect(page.locator(".app-shell-main--tablet-overlay")).toHaveCount(1);

        // Closed on arrival — an open overlay would cover chat.
        await expect(rightCol(page)).toHaveCount(0);
        const chatBefore = await width(page, ".chat-stage");

        // Its rail icon opens it OVER chat: chat keeps its width.
        await page.locator(".pl-rail--right .pl-rail__btn").first().tap();
        await expect(rightCol(page)).toBeVisible();
        expect(await width(page, ".chat-stage")).toBe(chatBefore);
        await expect(rightCol(page)).toHaveCSS("position", "absolute");
        // No divider to drag between chat and a floating panel.
        await expect(page.locator(".pl-appshell__handle")).toBeHidden();

        // A scrim covers chat while the panel floats; tapping it dismisses the panel (and
        // the scrim with it) without the tap landing on chat underneath.
        const scrim = page.getByTestId("tablet-scrim");
        await expect(scrim).toBeVisible();
        const lb = (await leftCol(page).boundingBox())!;
        const sb = (await scrim.boundingBox())!;
        expect(sb.width).toBeCloseTo(lb.width, 0);
        expect(await scrim.evaluate((el) => getComputedStyle(el).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)");
        await page.touchscreen.tap(lb.x + 40, lb.y + lb.height / 2);
        await expect(rightCol(page)).toHaveCount(0);
        await expect(scrim).toHaveCount(0);
      });

      // Opens the operator did not ask for follow the phone rule (ADR 0086, lib/autoOpen.ts):
      // the chip lands in chat and the panel stays closed until it is tapped.
      test("portrait: show_code never auto-opens the panel; tapping the chip does", async ({ page }) => {
        await withCodePane(page);
        await page.goto("/app/", { waitUntil: "load" });
        await expect(rightCol(page)).toHaveCount(0);
        await sendMessage(page, "SHOWCODE: on the tablet");

        const chip = page.getByTestId("code-ref-chip");
        await expect(chip).toBeVisible();
        await page.waitForTimeout(400);
        await expect(rightCol(page)).toHaveCount(0);
        await expect(page.getByTestId("tablet-scrim")).toHaveCount(0);

        await chip.tap();
        await expect(rightCol(page)).toBeVisible();
        await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
        await expect(page.getByTestId("tablet-scrim")).toBeVisible();
      });

      test("portrait: an artifact-ref never auto-opens the panel", async ({ page }) => {
        await withArtifactPanel(page);
        await page.goto("/app/", { waitUntil: "load" });
        await expect(rightCol(page)).toHaveCount(0);
        await sendMessage(page, "ARTIFACTREF V1 on the tablet");

        await expect(page.getByTestId("artifact-ref-chip")).toBeVisible();
        await page.waitForTimeout(400);
        await expect(rightCol(page)).toHaveCount(0);
      });

      test("portrait → landscape restores the split it collapsed", async ({ page }) => {
        await page.goto("/app/", { waitUntil: "load" });
        await expect(rightCol(page)).toHaveCount(0);

        const vp = page.viewportSize()!;
        await page.setViewportSize({ width: vp.height, height: vp.width });
        await expect(page.locator(".app-shell-main--tablet-overlay")).toHaveCount(0);
        await expect(rightCol(page)).toBeVisible();
        await expect(rightCol(page)).not.toHaveCSS("position", "absolute");
        await expect(page.getByRole("separator", { name: "Resize panels" })).toBeVisible();
      });
    } else {
      test("landscape: the dual-rail split stays", async ({ page }) => {
        await page.goto("/app/", { waitUntil: "load" });
        await expect(page.locator(".app-shell-main--tablet-overlay")).toHaveCount(0);
        await expect(rightCol(page)).toBeVisible();
        await expect(page.getByRole("separator", { name: "Resize panels" })).toBeVisible();
        await expect(page.getByTestId("tablet-scrim")).toHaveCount(0);
      });

      test("landscape: show_code still auto-opens the pane beside chat", async ({ page }) => {
        await withCodePane(page);
        await page.goto("/app/", { waitUntil: "load" });
        await sendMessage(page, "SHOWCODE: on the tablet");
        await expect(page.getByTestId("code-ref-chip")).toBeVisible();
        await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
        await expect(rightCol(page)).not.toHaveCSS("position", "absolute");
        await expect(page.getByTestId("tablet-scrim")).toHaveCount(0);
      });
    }
  });
}

// The overlay keys on INPUT, not just width: a portrait-shaped window driven by a mouse (a
// narrow desktop window, an iPad with a trackpad) keeps the split it has always had.
test.describe("portrait-shaped window with a mouse", () => {
  test.use({ viewport: { width: 820, height: 1180 }, hasTouch: false, isMobile: false });

  test("keeps the split", async ({ page }) => {
    await page.goto("/app/", { waitUntil: "load" });
    await expect(page.locator(".app-shell-main--tablet-overlay")).toHaveCount(0);
    await expect(rightCol(page)).toBeVisible();
    await expect(rightCol(page)).not.toHaveCSS("position", "absolute");
  });
});
