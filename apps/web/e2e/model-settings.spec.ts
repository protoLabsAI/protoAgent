import { expect, test } from "@playwright/test";

// Settings ▸ Model. Since ADR 0106 the page leads with Connections — the registry of
// model sources — and no longer carries the Provider / API base URL / API key fields or
// the single-gateway "Get models" probe, all of which assumed exactly one gateway.

async function openModelSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  // Field groups start collapsed — open them so the model field + actions are visible.
  const triggers = page.locator(".pl-accordion__trigger");
  await expect(triggers.first()).toBeVisible();
  for (let i = 0; i < (await triggers.count()); i++) {
    const t = triggers.nth(i);
    if ((await t.getAttribute("aria-expanded")) !== "true") await t.click();
  }
}

// The two "Get models" specs that lived here are gone with the button (ADR 0106).
// It probed "the form's gateway" — a question with no answer once several connections
// can be registered — so the probe moved onto the connection row, covered below.
test("Connections lists every registered connection, and the retired fields are gone", async ({ page }) => {
  await openModelSettings(page);

  const panel = page.getByTestId("providers-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("provider-row")).toHaveCount(2);
  await expect(panel).toContainText("Production gateway");
  await expect(panel).toContainText("local-vllm");
  // Two OpenAI-compatible endpoints at once — the shape a single api_base/api_key pair
  // could not express, and the reason the registry exists.
  await expect(panel).toContainText("https://gateway.example/v1");
  await expect(panel).toContainText("http://localhost:8000/v1");

  // Why a delete may be refused, shown before it is attempted.
  await expect(panel).toContainText("In use by 1 slot");
  // A connection with no key says so rather than looking ready.
  await expect(panel).toContainText("no API key");

  // The fields Connections replaced must not also be on the page — two editors for one
  // value is the contradiction this panel resolved.
  const dialog = page.locator(".settings-overlay");
  await expect(dialog).not.toContainText("API base URL");
  await expect(dialog).not.toContainText("Get models");
});

test("Add a connection submits Claude and Codex as native subscription types", async ({ page }) => {
  await openModelSettings(page);
  const panel = page.getByTestId("providers-panel");

  for (const expected of [
    { option: "Claude subscription", id: "anthropic-oauth", type: "anthropic-oauth", label: "Claude" },
    { option: "ChatGPT / Codex subscription", id: "openai-codex", type: "openai-codex", label: "ChatGPT / Codex" },
  ]) {
    await panel.getByRole("button", { name: "Add a connection", exact: true }).click();
    await panel.locator("#provider-connection-type").click();
    await page.getByRole("menuitemradio", { name: expected.option, exact: true }).click();

    // OAuth providers get useful stable ids and never ask for gateway credentials.
    // The field help is part of each native label's accessible name, hence the anchored
    // role match rather than an exact bare-label match.
    await expect(panel.getByRole("textbox", { name: /^Id\b/ })).toHaveValue(expected.id);
    await expect(panel.getByRole("textbox", { name: /^Name\b/ })).toHaveValue(expected.label);
    await expect(panel.getByRole("textbox", { name: /^Base URL\b/ })).toHaveCount(0);
    await expect(panel.getByRole("textbox", { name: /^API key\b/ })).toHaveCount(0);

    const posted = page.waitForRequest((request) =>
      request.method() === "POST" && new URL(request.url()).pathname === "/api/config/providers"
    );
    await panel.getByRole("button", { name: "Add connection", exact: true }).click();
    const body = (await posted).postDataJSON();
    expect(body).toEqual({ id: expected.id, type: expected.type, label: expected.label, base_url: "", api_key: "" });
  }
});

test("A gateway stays listed after add, settings reopen, and a fresh GET", async ({ page }) => {
  const providers = [
    {
      id: "existing",
      type: "openai-compat",
      label: "Existing",
      base_url: "https://existing.example/v1",
      display: "Existing",
      has_key: true,
      in_use_by: [],
    },
  ];
  await page.route("**/api/config/providers", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      providers.push({
        ...body,
        display: body.label || body.id,
        has_key: Boolean(body.api_key),
        in_use_by: [],
      });
      return route.fulfill({ json: { ok: true, id: body.id } });
    }
    return route.fulfill({ json: { providers } });
  });

  await openModelSettings(page);
  const panel = page.getByTestId("providers-panel");
  await panel.getByRole("button", { name: "Add a connection", exact: true }).click();
  await panel.getByRole("textbox", { name: /^Id\b/ }).fill("launch-gateway");
  await panel.getByRole("textbox", { name: /^Name\b/ }).fill("Launch gateway");
  await panel.getByRole("textbox", { name: /^Base URL\b/ }).fill("https://launch.example/v1");
  await panel.locator('input[type="password"]').fill("sk-launch");
  await panel.getByRole("button", { name: "Add connection", exact: true }).click();

  await expect(panel.getByTestId("provider-row")).toHaveCount(2);
  await expect(panel).toContainText("Launch gateway");

  // Reopening remounts the panel and forces the assertion through a new registry GET,
  // not React's existing row. This is the user-visible v0.150 regression contract.
  await page.keyboard.press("Escape");
  await expect(page.locator(".settings-overlay")).toBeHidden();
  await openModelSettings(page);
  await expect(page.getByTestId("providers-panel")).toContainText("Launch gateway");
});

test("Removing the last unused connection requires an explicit confirmation", async ({ page }) => {
  let present = true;
  await page.route("**/api/config/providers**", async (route) => {
    if (route.request().method() === "DELETE") {
      expect(new URL(route.request().url()).searchParams.get("confirm_last")).toBe("true");
      present = false;
      return route.fulfill({ json: { ok: true, removed: "only" } });
    }
    return route.fulfill({
      json: {
        providers: present
          ? [{ id: "only", type: "openai-compat", display: "Only", has_key: true, in_use_by: [] }]
          : [],
      },
    });
  });

  await openModelSettings(page);
  await page.getByRole("button", { name: "Remove Only" }).click();
  const dialog = page.getByRole("dialog", { name: "Remove the last connection?" });
  await expect(dialog).toContainText("no configured model source");
  await dialog.getByRole("button", { name: "Remove connection", exact: true }).click();
  await expect(page.getByTestId("provider-row")).toHaveCount(0);
});

test("Escape closes the dropdown first, Settings second — never both at once (#2466)", async ({ page }) => {
  await openModelSettings(page);
  const overlay = page.locator(".settings-overlay");
  const model = page.locator("#set-model\\.name");

  // Primary model is a live dropdown on load now — it offers every connection's models
  // (ADR 0106), where before it needed "Get models" to populate the one gateway's list.
  await model.click();
  await expect(page.getByRole("menuitemradio", { name: "local-vllm:qwen3-32b" })).toBeVisible();

  // FIRST Escape: only the topmost layer goes.
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menuitemradio", { name: "local-vllm:qwen3-32b" })).toBeHidden();
  await expect(overlay).toBeVisible();
  // ...still on Model, with the section's controls intact — the context the bug destroyed.
  await expect(page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }))
    .toHaveAttribute("aria-selected", "true");
  await expect(model).toBeVisible();

  // SECOND Escape, no nested layer open: now Settings closes.
  await page.keyboard.press("Escape");
  await expect(overlay).toBeHidden();
});
