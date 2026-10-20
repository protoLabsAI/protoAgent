import { expect, test, type Page } from "@playwright/test";

// The transcript follows the stream (B6). A user parked at the bottom keeps following as
// content grows — INCLUDING growth that lands after the follow scroll was issued, which is
// what a tall artifact card / iframe does when it mounts or measures. Following stops only
// when the user scrolls up, and sending a message always re-pins.
//
// The race this pins down: the follower's ResizeObserver sets scrollTop = scrollHeight; the
// resulting `scroll` event is dispatched a frame later; if a tall card grew the thread in
// between, that event measured "far from the bottom" and unpinned the view — so the next
// turns landed off-screen behind the jump button.

async function open(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByPlaceholder(/Message protoAgent/i).waitFor({ state: "visible" });
}

async function send(page: Page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill(prompt);
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: prompt })).toBeVisible();
}

/** Distance from the bottom of the visible transcript scroller. */
function distFromBottom(page: Page) {
  return page.evaluate(() => {
    const el = [...document.querySelectorAll<HTMLElement>(".pl-convo-scroll")].find((e) => e.offsetParent);
    if (!el) return -1;
    return Math.round(el.scrollHeight - el.scrollTop - el.clientHeight);
  });
}

/** Grow the transcript by `px` the way a late-measuring card does: a block appended to the
 *  message column, outside React's reconciliation (it only ever appends below it). */
async function grow(page: Page, px: number) {
  await page.evaluate((h) => {
    const convo = [...document.querySelectorAll<HTMLElement>(".pl-convo")].find((e) => e.offsetParent);
    const block = document.createElement("div");
    block.className = "e2e-late-growth";
    block.style.height = `${h}px`;
    block.style.flex = "none";
    convo!.appendChild(block);
  }, px);
}

/** Arm a one-shot "tall card mounts right after the follow scroll": the next time the
 *  column resizes, grow it again by `px` from a fresh task — after the follower already
 *  scrolled but before the browser dispatches that scroll's event. */
async function armLateGrowth(page: Page, px: number) {
  await page.evaluate((h) => {
    const convo = [...document.querySelectorAll<HTMLElement>(".pl-convo")].find((e) => e.offsetParent)!;
    let first = true;
    const ro = new ResizeObserver(() => {
      // observe() always delivers an initial notification — that one isn't a resize.
      if (first) {
        first = false;
        return;
      }
      ro.disconnect();
      setTimeout(() => {
        const block = document.createElement("div");
        block.className = "e2e-late-growth";
        block.style.height = `${h}px`;
        block.style.flex = "none";
        convo.appendChild(block);
      }, 0);
    });
    // Registered AFTER the transcript's own observer, so its callback runs second.
    requestAnimationFrame(() => ro.observe(convo));
  }, px);
  // Let the observer's initial (registration) notification pass before arming fires for real.
  await page.waitForTimeout(100);
}

const jump = (page: Page) => page.locator(".pl-convo__jump:visible");

test("a pinned transcript keeps following when a tall card grows after the follow scroll", async ({ page }) => {
  await open(page);
  await send(page, "first");
  await grow(page, 1500); // overflow the viewport
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);

  for (let i = 0; i < 3; i += 1) {
    await armLateGrowth(page, 450);
    await grow(page, 60);
    await page.waitForTimeout(250);
    await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  }
  await expect(jump(page)).toHaveCount(0);

  // ...and the next turn lands in view.
  await send(page, "second");
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  await expect(jump(page)).toHaveCount(0);
});

test("scrolling up stops following; sending a message re-pins", async ({ page }) => {
  await open(page);
  await send(page, "first");
  await grow(page, 1500);
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);

  // The user scrolls up: growth no longer drags them down.
  await page.locator(".pl-convo-scroll:visible").hover();
  await page.mouse.wheel(0, -700);
  await expect.poll(() => distFromBottom(page)).toBeGreaterThan(300);
  await expect(jump(page)).toHaveCount(1);
  const before = await distFromBottom(page);
  await grow(page, 400);
  await page.waitForTimeout(250);
  expect(await distFromBottom(page)).toBeGreaterThanOrEqual(before + 300);

  // Sending always re-pins — the new turn (and its answer) land in view.
  await send(page, "second");
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  await expect(jump(page)).toHaveCount(0);
  await grow(page, 500);
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
});

test("the jump button re-pins and following resumes", async ({ page }) => {
  await open(page);
  await send(page, "first");
  await grow(page, 1500);
  await page.locator(".pl-convo-scroll:visible").hover();
  await page.mouse.wheel(0, -700);
  await expect(jump(page)).toHaveCount(1);
  await jump(page).click();
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  await expect(jump(page)).toHaveCount(0);
  await armLateGrowth(page, 450);
  await grow(page, 60);
  await page.waitForTimeout(250);
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
});

test("a card shrinking above the viewport doesn't read as the user scrolling away", async ({ page }) => {
  // A streamed preview settling into its smaller final frame shrinks content ABOVE the
  // bottom; browser scroll anchoring then moves scrollTop up to hold its anchor — which,
  // unguarded, looked exactly like the user scrolling up and parked the view.
  await open(page);
  await send(page, "first");
  await page.evaluate(() => {
    const convo = [...document.querySelectorAll<HTMLElement>(".pl-convo")].find((e) => e.offsetParent)!;
    for (const [id, h] of [["e2e-card", 1500], ["e2e-tail", 300]] as const) {
      const block = document.createElement("div");
      block.id = id;
      block.style.height = `${h}px`;
      block.style.flex = "none";
      convo.appendChild(block);
    }
  });
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  // The card above settles 200px shorter while the tail below grows 160px, in one task.
  for (let i = 0; i < 3; i += 1) {
    await page.evaluate(() => {
      const card = document.getElementById("e2e-card")!;
      const tail = document.getElementById("e2e-tail")!;
      card.style.height = `${card.offsetHeight - 200}px`;
      tail.style.height = `${tail.offsetHeight + 160}px`;
    });
    await page.waitForTimeout(200);
    await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  }
  await grow(page, 400);
  await expect.poll(() => distFromBottom(page)).toBeLessThan(32);
  await expect(jump(page)).toHaveCount(0);
});
