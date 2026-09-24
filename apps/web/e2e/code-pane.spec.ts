import { expect, test, type Page } from "@playwright/test";

import { expandToolCard } from "./toolcard";

// The code pane (ADR 0112): a read-only file + diff viewer docked beside chat. The agent
// points (show_code → a `code-ref` chip that auto-opens the pane on the live stream), tool
// results link their paths into it, and the Diff tab shows the working tree vs HEAD. Mock
// data: e2e/codeFixtures.mjs.

async function send(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

/** The pierre host renders rows inside a shadow root — count the rows matching `sel`. */
async function shadowCount(page: Page, sel: string): Promise<number> {
  return page.locator('[data-testid="code-pane-body"] diffs-container').evaluate(
    (el, s) => el.shadowRoot?.querySelectorAll(s).length ?? 0,
    sel,
  );
}

/** Is row `line` inside the pane body's visible box? */
async function lineVisible(page: Page, line: number): Promise<boolean> {
  return page.locator('[data-testid="code-pane-body"]').evaluate((body, n) => {
    const row = body.querySelector("diffs-container")?.shadowRoot?.querySelector(`[data-line="${n}"]`);
    if (!row) return false;
    const b = body.getBoundingClientRect();
    const r = row.getBoundingClientRect();
    return r.top >= b.top && r.bottom <= b.bottom;
  }, line);
}

test("show_code: the chip renders and the pane opens at the range, with the note", async ({ page }) => {
  await send(page, "SHOWCODE: show me the auth check");

  const chip = page.getByTestId("code-ref-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("src/server.ts:23-29");
  await expect(chip).toContainText("constant-time");

  // Live stream → the pane auto-opens (desktop), on the dock that isn't chat's.
  const pane = page.getByTestId("code-pane");
  await expect(pane).toBeVisible();
  await expect(page.locator(".pl-appshell__col--right").getByTestId("code-pane")).toBeVisible();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  await expect(page.getByTestId("code-pane-range")).toHaveText("L23–29");
  await expect(page.getByTestId("code-pane-note")).toContainText("XOR-folded");

  // The range is highlighted (number + content cell per line) and scrolled into view.
  await expect.poll(() => shadowCount(page, "[data-selected-line]")).toBeGreaterThanOrEqual(7);
  await expect.poll(() => lineVisible(page, 23)).toBe(true);
});

test("a reload does NOT re-open the pane from the transcript (live stream only)", async ({ page }) => {
  await send(page, "SHOWCODE: once");
  await expect(page.getByTestId("code-pane")).toBeVisible();
  // Switch the right dock back to Work, reload: the hydrated chip must not steal it again.
  await page.getByRole("button", { name: "Work", exact: true }).first().click();
  await expect(page.getByTestId("code-pane")).toHaveCount(0);
  await page.reload({ waitUntil: "load" });
  await expect(page.getByTestId("code-ref-chip")).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.getByTestId("code-pane")).toHaveCount(0);
  // …but the chip still opens it on a click.
  await page.getByTestId("code-ref-chip").click();
  await expect(page.getByTestId("code-pane-range")).toHaveText("L23–29");
});

test("a read_file path link opens the pane at the read's lines", async ({ page }) => {
  await send(page, "READFILE: look at the handler");
  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await expandToolCard(page, card);
  const link = page.locator("a.tool-editor-link").first();
  await expect(link).toBeVisible();
  await expect(link).toHaveText("src/server.ts:34");
  await link.click();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  await expect(page.getByTestId("code-pane-range")).toHaveText("L34–45");
  await expect.poll(() => lineVisible(page, 34)).toBe(true);
});

test("Diff tab: file list with +/- counts, a hidden secret, and a click opens the line", async ({ page }) => {
  await send(page, "SHOWCODE: diff please");
  await expect(page.getByTestId("code-pane")).toBeVisible();
  await page.getByTestId("code-pane").getByRole("tab", { name: "Diff", exact: true }).click();

  const list = page.getByTestId("code-pane-files");
  await expect(list.locator("button")).toHaveCount(4);
  await expect(list.locator("button").first()).toContainText("src/server.ts");
  await expect(list.locator("button").first()).toContainText("+6");
  await expect(list.locator("button").first()).toContainText("−1");
  const secret = list.locator("button", { hasText: ".env" });
  await expect(secret).toBeDisabled();
  await expect(secret).toContainText("hidden");
  await expect(page.locator(".code-pane__branch")).toContainText("feat/constant-time-auth");

  // The picked file's patch renders; a click on an added row opens it in the File tab.
  const diffHost = page.locator(".code-pane__body--diff diffs-container");
  await expect.poll(() => diffHost.evaluate((el) => el.shadowRoot?.querySelectorAll("[data-line]").length ?? 0)).toBeGreaterThan(0);
  await diffHost.evaluate((el) => {
    const rows = [...(el.shadowRoot?.querySelectorAll<HTMLElement>("[data-line-type='change-addition'][data-line]") ?? [])];
    const row = rows.find((r) => r.getAttribute("data-line") === "25") ?? rows[0];
    row?.click();
  });
  await expect(page.getByTestId("code-pane").getByRole("tab", { name: "File", exact: true, selected: true })).toBeVisible();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  await expect(page.getByTestId("code-pane-range")).toHaveText(/^L2\d$/);

  // Another file in the list swaps the patch.
  await page.getByTestId("code-pane").getByRole("tab", { name: "Diff", exact: true }).click();
  await list.locator("button", { hasText: "notes/todo.md" }).click();
  await expect
    .poll(() => diffHost.evaluate((el) => el.shadowRoot?.textContent?.includes("rate-limit") ?? false))
    .toBe(true);
});

for (const [prompt, testId, text] of [
  ["SHOWSECRET", "code-pane-denied", "Hidden: secret-like file"],
  ["SHOWGONE", "code-pane-gone", "no longer exists"],
  ["SHOWBINARY", "code-pane-binary", "Binary file"],
] as const) {
  test(`error state: ${prompt}`, async ({ page }) => {
    await send(page, `${prompt}: point`);
    await expect(page.getByTestId(testId)).toContainText(text);
  });
}

test("a 20k-line file opens fast at a deep line (virtualized; plain past the highlight cap)", async ({ page }) => {
  await send(page, "SHOWHUGE: deep");
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/huge.ts");
  await expect.poll(() => lineVisible(page, 15000), { timeout: 10_000 }).toBe(true);
  // Only a window of rows is in the DOM, not 20k of them.
  expect(await shadowCount(page, "[data-line]")).toBeLessThan(2000);
});

test("follow mode (opt-in): a completed read_file moves the pane; the pin holds it", async ({ page }) => {
  await send(page, "SHOWCODE: start");
  await expect(page.getByTestId("code-pane-range")).toHaveText("L23–29");

  // OFF by default: a read doesn't move the pane.
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(page.getByTestId("code-follow")).toHaveAttribute("aria-pressed", "false");
  await composer.fill("FOLLOWREAD: one");
  await composer.press("Enter");
  await expect(page.getByText("Read the generated rows.").first()).toBeVisible();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");

  // ON: the next read lands the pane on it, scrolled to the range.
  await page.getByTestId("code-follow").click();
  await expect(page.getByTestId("code-follow")).toHaveAttribute("aria-pressed", "true");
  await composer.fill("FOLLOWREAD: two");
  await composer.press("Enter");
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/big.ts");
  await expect(page.getByTestId("code-pane-range")).toHaveText("L2400–2419");
  await expect.poll(() => lineVisible(page, 2400), { timeout: 15_000 }).toBe(true);

  // Pinned: follow doesn't move away. (Revisit server.ts from the trail, then read again.)
  await page.getByTestId("code-pane-recent").getByRole("button", { name: /Recent/ }).click();
  await page.locator(".code-pane__recent-item", { hasText: "src/server.ts:23-29" }).click();
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
  await page.getByTestId("code-pin").click();
  await page.waitForTimeout(900); // past the follow throttle
  await composer.fill("FOLLOWREAD: three");
  await composer.press("Enter");
  await expect(page.getByText("Read the generated rows.").nth(2)).toBeVisible();
  await page.waitForTimeout(900);
  await expect(page.getByTestId("code-pane-path")).toHaveText("src/server.ts");
});
