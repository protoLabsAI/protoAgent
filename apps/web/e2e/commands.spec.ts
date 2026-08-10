import { expect, test } from "@playwright/test";

import { SLASH_COMMANDS } from "./fixtures.mjs";

// The chat composer fetches the server's registered slash commands
// (GET /api/chat/commands) and autocompletes them as you type "/name".

// Deterministic client-side commands (ADR 0057) surface FIRST, then the server skills.
// `/goal` is a client command that claims only `/goal new` (a guided goal form, ADR 0073) —
// everything else falls through to the SERVER `/goal`. The menu DEDUPS by token, so a command
// that's both a client command and a server skill (`/goal`, `/clear`) appears ONCE, client-first
// — the server duplicate is dropped.
const CLIENT_SLASH = ["/new", "/clear", "/export", "/btw", "/prompt", "/compact", "/effort", "/model", "/incognito", "/help", "/bypass", "/goal", "/watch"];
// The server rows the menu shows, with client-token duplicates deduped away.
const serverRows = () => SLASH_COMMANDS.map((c) => `/${c.name}`).filter((n) => !CLIENT_SLASH.includes(n));

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("slash menu opens and lists the client + server commands", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  // Each command renders as a `.slash-name` row (the description can repeat the
  // name, so scope to the name span to avoid matching twice). Client commands first.
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toEqual([...CLIENT_SLASH, ...serverRows()]);
  // Workflows are listed as slash commands too (ADR 0002).
  expect(names).toContain("/research-and-brief");
  // Deduped: exactly one `/goal` and one `/clear`, not the client+server pair.
  expect(names.filter((n) => n === "/goal")).toHaveLength(1);
  expect(names.filter((n) => n === "/clear")).toHaveLength(1);
});

test("a flag-gated command (/compact, ADR 0068) vanishes when its flag is forced off", async ({ page }) => {
  // The ?flag: query override is the shareable "try this build" layer — here it turns the
  // chat.compact flag OFF over the mock server's enabled state, so /compact must not list.
  await page.goto("/app/?flag:chat.compact=off", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toEqual([...CLIENT_SLASH.filter((n) => n !== "/compact"), ...serverRows()]);
});

test("filtering narrows the menu and selecting completes the command", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/go");

  const menu = page.locator(".slash-menu");
  // "/go" matches a single `/goal` now — the client command and the server `/goal` are
  // deduped to one row (client wins). Bare `/goal` falls through, inserting `/goal ` to edit.
  await expect(menu.locator(".slash-item")).toHaveCount(1);
  await expect(menu.getByText("/goal", { exact: true }).first()).toBeVisible();

  // Enter completes the highlighted command into the composer.
  await composer.press("Enter");
  await expect(composer).toHaveValue("/goal ");
  // Completing closes the menu (a space follows the command).
  await expect(menu).toBeHidden();
});

test("clicking Send on a bare command closes the slash menu so the picker is mouse-usable (#2492)", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/effort");
  await expect(page.locator(".slash-menu")).toBeVisible();

  // The MOUSE path: clicking Send fires none of the textarea events the slash
  // popover refreshes on, so the stale menu used to stay mounted OVER the form
  // and intercept its clicks (".slash-title … intercepts pointer events").
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".slash-menu")).toBeHidden();

  const form = page.locator(".hitl-card", { hasText: "Reasoning effort" });
  await expect(form).toBeVisible();
  // Playwright fails a click whose hit target is covered — these clicks ARE the
  // regression assertion, not just setup.
  await form.locator(".hitl-card-option", { hasText: "low" }).click();
  await form.getByRole("button", { name: /submit/i }).click();
  await expect(form).toBeHidden();
});
