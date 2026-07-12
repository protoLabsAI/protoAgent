import { expect, test } from "@playwright/test";

// /model quick-switch (#1957): bare /model opens an inline card picker (the /effort
// pattern) fed by the PINNED FAVORITES from Settings ▸ Model ▸ Favorite models
// (settings-schema fixture: favorites ["protolabs/fast", "protolabs/reasoning"] —
// deliberately the REVERSE of the gateway options order, so favorites-driven ordering
// is observable). Picking a card switches the tab's model, which rides the next send
// as metadata.model — asserted at the WIRE level like incognito.spec. The Settings
// side (add/remove/reorder favorites) is covered here too.

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

function captureA2ABodies(page: import("@playwright/test").Page): { metadata?: Record<string, unknown> }[] {
  const messages: { metadata?: Record<string, unknown> }[] = [];
  page.on("request", (req) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      if (body?.method === "SendStreamingMessage") messages.push(body.params?.message ?? {});
    } catch {
      // non-JSON /a2a traffic — not a chat turn
    }
  });
  return messages;
}

test("bare /model shows ONLY the favorites as cards; picking one switches the model and rides the next send", async ({ page }) => {
  const sent = captureA2ABodies(page);
  const composer = page.getByPlaceholder(/Message protoAgent/i);

  await composer.fill("/model");
  await composer.press("Enter"); // picks the highlighted client command → opens the picker

  const form = page.locator(".hitl-card", { hasText: "Switch model" });
  await expect(form).toBeVisible();
  // The favorites, in the PINNED order (fast first — the reverse of the options order),
  // not the gateway's full list order.
  await expect(form.locator(".hitl-card-option .hitl-card-label")).toHaveText([
    "protolabs/fast",
    "protolabs/reasoning",
  ]);
  // Provider + configured-default hints on the cards (#1957 "name + provider").
  await expect(form.locator(".hitl-card-desc").last()).toHaveText("protolabs · configured default");

  // The single required field gates Submit until a card is picked (/effort-style form).
  const submit = form.getByRole("button", { name: "Submit" });
  await expect(submit).toBeDisabled();
  await form.getByRole("radio", { name: /protolabs\/fast/ }).click();
  await submit.click();
  await expect(page.locator(".chat-note", { hasText: "Model set to" })).toBeVisible();
  // The composer's per-tab model select reflects the switch immediately.
  await expect(page.getByRole("button", { name: "Model for this chat" })).toHaveText("protolabs/fast");

  // …and the next send carries the override on the wire (server/chat.py reads metadata.model).
  await composer.fill("hello there");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user", { hasText: "hello there" })).toBeVisible();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].metadata?.model).toBe("protolabs/fast");
});

test("no favorites configured → /model falls back to the FULL model list with a pin-favorites hint", async ({ page }) => {
  // Serve the same schema with the favorites emptied — the graceful-fallback state.
  await page.route("**/api/settings/schema", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    for (const g of json.groups) for (const f of g.fields ?? []) if (f.key === "model.favorites") f.value = [];
    await route.fulfill({ response, json });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/model");
  await composer.press("Enter");

  const form = page.locator(".hitl-card", { hasText: "Switch model" });
  await expect(form).toBeVisible();
  // Full gateway list, in the gateway's order this time.
  await expect(form.locator(".hitl-card-option .hitl-card-label")).toHaveText([
    "protolabs/reasoning",
    "protolabs/fast",
  ]);
  await expect(form.locator(".hitl-prompt")).toContainText("No favorites pinned");
});

test("typed /model <alias> switches directly, no form; /model default resets the override", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);

  await composer.fill("/model protolabs/fast");
  await composer.press("Enter");
  await expect(page.locator(".chat-note", { hasText: "Model set to" })).toBeVisible();
  await expect(page.locator(".hitl-card")).toHaveCount(0); // direct apply — no picker
  await expect(page.getByRole("button", { name: "Model for this chat" })).toHaveText("protolabs/fast");

  await composer.fill("/model default");
  await composer.press("Enter");
  await expect(page.locator(".chat-note", { hasText: "reset to the configured default" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Model for this chat" })).toHaveText("protolabs/reasoning");
});

test("Settings ▸ Model ▸ Favorite models: reorder via up/down and save the new order", async ({ page }) => {
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  await page.locator(".pl-accordion__trigger", { hasText: "Favorite models" }).click();

  const row = page.locator('.setting-row[data-key="model.favorites"]');
  const inputs = row.locator("input");
  await expect(inputs).toHaveCount(3); // two favorites + the trailing blank-to-add row
  await expect(inputs.nth(0)).toHaveValue("protolabs/fast");
  await expect(inputs.nth(1)).toHaveValue("protolabs/reasoning");

  // Simple up/down buttons (#1957 — deliberately not drag-and-drop).
  await row.getByRole("button", { name: "Move protolabs/fast down" }).click();
  await expect(inputs.nth(0)).toHaveValue("protolabs/reasoning");
  await expect(inputs.nth(1)).toHaveValue("protolabs/fast");

  // Save posts the reordered list to the agent leaf (model.favorites is agent-scoped).
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeEnabled();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/api/settings") && r.method() === "POST"),
    save.click(),
  ]);
  const body = req.postDataJSON() as { layer: string; updates: Record<string, unknown> };
  expect(body.updates["model.favorites"]).toEqual(["protolabs/reasoning", "protolabs/fast"]);
  expect(body.layer).toBe("agent");
  await expect(page.locator(".pl-toast", { hasText: "config saved" })).toBeVisible();
});
