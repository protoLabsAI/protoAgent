import { expect, test } from "@playwright/test";

// The keybinding system (ADR 0063): a scoped, user-rebindable global keyboard layer. In
// headless Chromium there's no browser chrome, so even browser-reserved combos (⌘T, ⌘1)
// reach the page. `ControlOrMeta` matches our `mod` (⌘ on mac / Ctrl elsewhere).

test("mod+K toggles the command palette", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-cmdk__panel")).toHaveCount(0);
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator(".pl-cmdk__panel")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator(".pl-cmdk__panel")).toHaveCount(0);
});

test("'/' focuses the chat composer when not already typing (global, gated)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  // Move focus OFF the composer (it autofocuses on load) onto a neutral rail button.
  await page.locator(".pl-rail").getByRole("button", { name: "Knowledge", exact: true }).focus();
  await expect(composer).not.toBeFocused();
  await page.keyboard.press("/");
  await expect(composer).toBeFocused();
});

test("mod+T opens a new chat tab (chat-scoped)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(1);
  await page.getByPlaceholder(/Message protoAgent/i).focus(); // focus inside the chat scope
  await page.keyboard.press("ControlOrMeta+t");
  await expect(tabs).toHaveCount(2);
});

test("mod+1 / mod+2 jump to chat tab N (chat-scoped)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  // The composer of the VISIBLE session slot (a 2nd tab mounts a 2nd slot/composer).
  const composer = () => page.locator(".chat-session-slot:not([hidden])").getByPlaceholder(/Message protoAgent/i);
  await composer().focus();
  await page.keyboard.press("ControlOrMeta+t"); // 2 tabs now
  await expect(tabs).toHaveCount(2);
  await composer().focus();
  await page.keyboard.press("ControlOrMeta+1");
  await expect(tabs.nth(0)).toHaveClass(/pl-tabbar__tab--active/);
  await composer().focus();
  await page.keyboard.press("ControlOrMeta+2");
  await expect(tabs.nth(1)).toHaveClass(/pl-tabbar__tab--active/);
});

test("chat-scoped shortcut does NOT fire when focus is outside the chat panel", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(1);
  // Focus a neutral element outside the chat scope, then press the chat-scoped New-chat combo.
  await page.locator(".pl-rail").getByRole("button", { name: "Knowledge", exact: true }).focus();
  await page.keyboard.press("ControlOrMeta+t");
  await expect(tabs).toHaveCount(1); // no new tab — chat scope wasn't focused
});

test("⌘B / ⌘⌥B toggle the left rail / right panel (global)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const left = page.locator(".pl-appshell__col--left");
  const right = page.locator(".pl-appshell__col--right");
  await expect(left).toHaveCount(1);
  await page.keyboard.press("ControlOrMeta+b");
  await expect(left).toHaveCount(0); // collapsed
  await page.keyboard.press("ControlOrMeta+b");
  await expect(left).toHaveCount(1); // back

  await expect(right).toHaveCount(1);
  await page.keyboard.press("ControlOrMeta+Alt+b");
  await expect(right).toHaveCount(0);
  await page.keyboard.press("ControlOrMeta+Alt+b");
  await expect(right).toHaveCount(1);
});

test("Settings ▸ Keyboard lists the bindings (opened via mod+,)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.keyboard.press("ControlOrMeta+Comma"); // settings.open
  await page.getByText("Keyboard", { exact: true }).click();
  await expect(page.getByText("Command palette", { exact: true })).toBeVisible();
  await expect(page.locator(".kb-row", { hasText: "New chat" })).toBeVisible();
});
