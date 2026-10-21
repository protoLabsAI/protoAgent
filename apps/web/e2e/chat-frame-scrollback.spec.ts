import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

const scroller = (page: Page) => page.locator(".pl-convo-scroll:visible");
const distance = (page: Page) => scroller(page).evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);

async function seedAnswer(page: Page) {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/refs?*", (route) => route.fulfill({ json: {
    artifacts: { "scrollback-frame": { title: "ADR 0118", kind: "html", version_count: 1, oldest: 1 } },
  } }));
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: {
    current: "scrollback-frame",
    artifacts: [{ id: "scrollback-frame", kind: "html", title: "ADR 0118", version_count: 1, created: 1, updated: 1,
      versions: [{ code: '<div style="height:600px;background:#def">ADR 0118 interactive answer</div>', ts: 1, by: "agent" }] }],
  } }));
  await page.addInitScript(() => {
    const text = (start: number) => Array.from({ length: 24 }, (_, i) =>
      `Answer block ${start + i}. This is a finished answer explaining interactive artifacts and their lifecycle. ` +
      "Each paragraph must remain readable while scrolling through the conversation.",
    ).join("\n\n");
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({ version: 1, currentSessionId: "frame-scrollback", sessions: [{
      id: "frame-scrollback", title: "Frame scrollback", createdAt: 1, updatedAt: 1,
      messages: [
        { id: "question", role: "user", content: "Explain ADR 0118", status: "done" },
        { id: "answer", role: "assistant", content: text(1) + "\n\n" + text(25), status: "done", parts: [
          { kind: "text", text: text(1) },
          { kind: "component", spec: { component: "artifact-ref", props: {
            artifact_id: "scrollback-frame", version: 1, title: "ADR 0118", kind: "html", inline: true, height: 600,
          } } },
          { kind: "text", text: text(25) },
        ] },
      ],
    }] }));
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".markdown p")).toHaveCount(48);
}

test("finished inline answer keeps its height and paints every block on scrollback (#4421)", async ({ page }, testInfo) => {
  await seedAnswer(page);
  // Visit the frame before leaving it, so its measured height is the baseline.
  await scroller(page).hover({ position: { x: 20, y: 20 } });
  const cardTop = await page.getByTestId("artifact-ref-inline").evaluate((el) => el.getBoundingClientRect().top);
  await page.mouse.wheel(0, cardTop - 200);
  await expect(page.getByTestId("artifact-inline-frame").contentFrame().frameLocator("#frame").getByText("ADR 0118 interactive answer")).toBeVisible();
  await page.waitForTimeout(300);
  const height = await scroller(page).evaluate((el) => el.scrollHeight);
  const cardHeight = await page.getByTestId("artifact-ref-inline").evaluate((el) => el.getBoundingClientRect().height);
  await page.screenshot({ path: testInfo.outputPath("before-scrollback.png") });
  await testInfo.attach("before-scrollback", { path: testInfo.outputPath("before-scrollback.png"), contentType: "image/png" });
  await scroller(page).hover({ position: { x: 20, y: 20 } });
  await page.mouse.wheel(0, 20000);
  await expect.poll(() => distance(page)).toBeLessThan(32);
  await page.mouse.wheel(0, -20000);
  await expect.poll(() => scroller(page).evaluate((el) => el.scrollTop)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath("scrollback-top.png") });
  await testInfo.attach("scrollback-top", { path: testInfo.outputPath("scrollback-top.png"), contentType: "image/png" });
  expect(await scroller(page).evaluate((el) => el.scrollHeight)).toBeGreaterThanOrEqual(height - 1);

  expect(await page.getByTestId("artifact-ref-inline").evaluate((el) => el.getBoundingClientRect().height)).toBe(cardHeight);

  const seen = new Set<string>();
  for (let step = 0; step < 30; step += 1) {
    const visible = await scroller(page).evaluate((el) => {
      const viewport = el.getBoundingClientRect();
      return [...el.querySelectorAll<HTMLElement>(".markdown p")].map((block) => {
        const rect = block.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(block);
        return { text: block.innerText, height: rect.height, textHeight: range.getBoundingClientRect().height,
          painted: block.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }), visible: rect.bottom > viewport.top && rect.top < viewport.bottom };
      }).filter((block) => block.visible);
    });
    for (const block of visible) {
      expect(block.height).toBeGreaterThan(0);
      expect(block.textHeight).toBeGreaterThan(0);
      expect(block.painted).toBe(true);
      expect(block.text).toMatch(/^Answer block \d+\./);
      seen.add(block.text);
    }
    expect(await scroller(page).evaluate((el) => el.scrollHeight)).toBeGreaterThanOrEqual(height - 1);
    if (await distance(page) < 2) break;
    await page.mouse.wheel(0, 350);
    await page.waitForTimeout(100);
  }
  expect(seen.size).toBe(48);
  await page.screenshot({ path: testInfo.outputPath("scrollback-bottom.png") });
  await testInfo.attach("scrollback-bottom", { path: testInfo.outputPath("scrollback-bottom.png"), contentType: "image/png" });
});

test("growth in several tool rows above a streaming message stays pinned (#4298)", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({ version: 1, currentSessionId: "tool-growth", sessions: [{
      id: "tool-growth", title: "Tool growth", createdAt: 1, updatedAt: 1,
      messages: [{ id: "tools", role: "assistant", content: "Earlier tool results", status: "done",
        toolCalls: Array.from({ length: 3 }, (_, i) => ({ id: `lookup-${i}`, name: `lookup_${i}`, status: "done", output: `Result ${i}` })),
      }],
    }] }));
  });
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await page.getByRole("button", { name: "done 3 tools", exact: true }).click();
  await composer.fill("hold the tool open");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--streaming")).toBeVisible();
  const rows = page.locator(".pl-toolcard--done");
  await expect(rows).toHaveCount(3);
  // As in chat-follow-stream.spec.ts, drive asynchronous measurement changes outside
  // React, here on three real tool cards ABOVE the live message.
  await rows.evaluateAll((cards) => cards.forEach((card) => {
    (card as HTMLElement).style.height = "500px";
    card.classList.add("e2e-tool-growth");
  }));
  await expect.poll(() => distance(page)).toBeLessThan(32);
  for (let round = 0; round < 4; round += 1) {
    await page.evaluate(() => {
      document.querySelectorAll<HTMLElement>(".e2e-tool-growth").forEach((row, i) => {
        setTimeout(() => { row.style.height = `${row.offsetHeight + 300}px`; }, i * 30);
      });
    });
    await page.waitForTimeout(200);
    await expect.poll(() => distance(page)).toBeLessThan(32);
    await expect(page.locator(".pl-convo__jump:visible")).toHaveCount(0);
    await expect(page.locator(".pl-message--streaming")).toBeVisible();
  }
});
