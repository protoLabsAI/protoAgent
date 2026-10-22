import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

const scroller = (page: Page) => page.locator(".pl-convo-scroll:visible");
const distance = (page: Page) => scroller(page).evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);

// Keep successful attempts too: a non-reproduction is not evidence of a repair.
test.use({ deviceScaleFactor: 2, video: "on", trace: "on" });

async function paintedParagraphs(page: Page, testInfo: TestInfo, name: string) {
  const blocks = await scroller(page).evaluate((el) => {
    const viewport = el.getBoundingClientRect();
    return [...el.querySelectorAll<HTMLElement>(".markdown p")].flatMap((block) => {
      const rect = block.getBoundingClientRect();
      // Judge whole paragraphs; each must pass during the overlapping sweep.
      if (rect.top < viewport.top || rect.bottom > viewport.bottom) return [];
      return [{ text: block.innerText, x: rect.x, y: rect.y, width: rect.width, height: rect.height }];
    });
  });
  // No locator scrolling or style changes: DOM geometry locates the samples,
  // but only viewport screenshot pixels determine whether text was painted.
  const shot = await page.screenshot({ path: testInfo.outputPath(`${name}.png`), scale: "device" });
  await testInfo.attach(name, { body: shot, contentType: "image/png" });
  const samples = await page.evaluate(async ({ png, blocks }) => {
    const bytes = Uint8Array.from(atob(png), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();
    const dpr = window.devicePixelRatio;
    return blocks.map((block) => {
      const { data } = ctx.getImageData(Math.ceil(block.x * dpr), Math.ceil(block.y * dpr),
        Math.floor(block.width * dpr), Math.floor(block.height * dpr));
      // Plain paragraphs have a uniform background. Count contrasting ink pixels
      // instead of accepting nonzero boxes or checkVisibility as painted text.
      let ink = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (Math.max(Math.abs(data[i] - data[0]), Math.abs(data[i + 1] - data[1]),
          Math.abs(data[i + 2] - data[2])) > 40) ink += 1;
      }
      return { text: block.text, inkFraction: ink / (data.length / 4) };
    });
  }, { png: shot.toString("base64"), blocks });
  await testInfo.attach(`${name}-pixels`, { body: JSON.stringify(samples), contentType: "application/json" });
  for (const sample of samples) {
    expect(sample.inkFraction, `${name}: ${sample.text}`).toBeGreaterThan(0.01);
  }
  return samples.map((sample) => sample.text);
}

async function seedAnswer(page: Page, paragraphsPerHalf = 24) {
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
  await page.addInitScript((paragraphsPerHalf) => {
    const text = (start: number) => Array.from({ length: paragraphsPerHalf }, (_, i) =>
      `Answer block ${start + i}. This is a finished answer explaining interactive artifacts and their lifecycle. ` +
      "Each paragraph must remain readable while scrolling through the conversation.",
    ).join("\n\n");
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({ version: 1, currentSessionId: "frame-scrollback", sessions: [{
      id: "frame-scrollback", title: "Frame scrollback", createdAt: 1, updatedAt: 1,
      messages: [
        { id: "question", role: "user", content: "Explain ADR 0118", status: "done" },
        { id: "answer", role: "assistant", content: text(1) + "\n\n" + text(paragraphsPerHalf + 1), status: "done", parts: [
          { kind: "text", text: text(1) },
          { kind: "component", spec: { component: "artifact-ref", props: {
            artifact_id: "scrollback-frame", version: 1, title: "ADR 0118", kind: "html", inline: true, height: 600,
          } } },
          { kind: "text", text: text(paragraphsPerHalf + 1) },
        ] },
      ],
    }] }));
  }, paragraphsPerHalf);
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".markdown p")).toHaveCount(paragraphsPerHalf * 2);
}

// Also cross large raster surface sizes with a substantially longer answer.
for (const paragraphsPerHalf of [24, 96]) {
  test(`finished inline answer paints ${paragraphsPerHalf * 2} blocks on scrollback at DPR 2 (#4421)`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await seedAnswer(page, paragraphsPerHalf);
    expect(await page.evaluate(() => window.devicePixelRatio)).toBe(2);
    await testInfo.attach("environment", { contentType: "application/json", body: JSON.stringify({
      browser: page.context().browser()?.version(), viewport: page.viewportSize(),
      dpr: await page.evaluate(() => window.devicePixelRatio), platform: process.platform,
      arch: process.arch, node: process.version,
    }) });
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
    for (let step = 0; step < paragraphsPerHalf * 2; step += 1) {
      const visible = await scroller(page).evaluate((el) => {
        const viewport = el.getBoundingClientRect();
        return [...el.querySelectorAll<HTMLElement>(".markdown p")].map((block) => {
          const rect = block.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(block);
          return { text: block.innerText, height: rect.height, textHeight: range.getBoundingClientRect().height,
            cssVisible: block.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }), visible: rect.bottom > viewport.top && rect.top < viewport.bottom };
        }).filter((block) => block.visible);
      });
      for (const block of visible) {
        expect(block.height).toBeGreaterThan(0);
        expect(block.textHeight).toBeGreaterThan(0);
        expect(block.cssVisible).toBe(true);
        expect(block.text).toMatch(/^Answer block \d+\./);
      }
      for (const text of await paintedParagraphs(page, testInfo, `scrollback-${step}`)) seen.add(text);
      expect(await scroller(page).evaluate((el) => el.scrollHeight)).toBeGreaterThanOrEqual(height - 1);
      if (await distance(page) < 2) break;
      await page.mouse.wheel(0, 350);
      await page.waitForTimeout(100);
    }
    expect(seen.size).toBe(paragraphsPerHalf * 2);
    await page.screenshot({ path: testInfo.outputPath("scrollback-bottom.png") });
    await testInfo.attach("scrollback-bottom", { path: testInfo.outputPath("scrollback-bottom.png"), contentType: "image/png" });
  });
}

test("paint oracle rejects blank paragraphs even when their DOM boxes remain", async ({ page }, testInfo) => {
  await seedAnswer(page);
  expect((await paintedParagraphs(page, testInfo, "oracle-readable")).length).toBeGreaterThan(0);
  // Negative control only, not a reproduction of #4421: layout/text remain intact.
  await page.addStyleTag({ content: ".markdown p { visibility: hidden !important; }" });
  await expect(paintedParagraphs(page, testInfo, "oracle-deliberately-blank")).rejects.toThrow("toBeGreaterThan");
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
