import { expect, test, type Page } from "@playwright/test";

const base = { version: 1, title: "Design example", created_at: "2026-10-13T00:00:00Z" };
const palette = { ...base, kind: "palette", groups: [{ name: "Semantic tokens", swatches: [
  { label: "--pl-color-fg", value: "#ffffff", light: "#000000", copy: "var(--pl-color-fg)" },
  { label: "--pl-color-accent", value: "#9b87f2", light: "#5b3fc9", copy: "var(--pl-color-accent)" },
] }] };

async function send(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("DESIGN-PREVIEW please");
  await composer.press("Enter");
  await expect(page.getByText("Here is the design preview.")).toBeVisible();
  return page.locator(".design-preview");
}

test("palette streams inline, copies values, and restores its snapshot after reload", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route("**/api/plugins/design-system/previews/*", route => route.fulfill({ json: palette }));
  const card = await send(page);
  await expect(card).toContainText("Semantic tokens");
  await expect(card.locator(".design-preview__color").first()).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await card.getByRole("button", { name: "Light", exact: true }).click();
  await card.getByRole("button", { name: "Copy value: #000000", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe("#000000");
  await card.getByRole("button", { name: "Copy token: var(--pl-color-fg)", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe("var(--pl-color-fg)");
  await page.reload();
  await expect(card).toContainText("Saved preview");
  await expect(card.locator(".design-preview__color").first()).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card).toBeVisible();
  expect(await card.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
});

test("component variants stay inline and a click delivers the selected story to a closed playground", async ({ page }) => {
  const stories = ["primary", "ghost"].map(name => ({ id: `button--${name}`, name,
    preview: `https://stories.example.com/iframe?id=button--${name}`,
    docs: `https://stories.example.com/?path=/story/button--${name}` }));
  await page.route("**/api/plugins/design-system/previews/*", route => route.fulfill({ json: {
    ...base, kind: "component", component_title: "Primitives/Button", storybook_url: "https://stories.example.com", stories,
  } }));
  await page.route("https://stories.example.com/**", route => route.fulfill({ contentType: "text/html", body: "<button>Real story fixture</button>" }));
  await page.route("**/api/runtime/status", async route => {
    const res = await route.fetch();
    const data = await res.json();
    await route.fulfill({ json: { ...data, plugins: [...data.plugins, {
      id: "design-system", name: "Design System", version: "0.8.0", enabled: true, loaded: true, tools: [],
      views: [{ id: "design-system", label: "Design System", icon: "Component", placement: "right", path: "/plugins/design-system/view" }],
    }] } });
  });
  // The panel handshake is real; this fixture records the queued plugin-owned message.
  await page.route("**/plugins/design-system/view*", route => route.fulfill({ contentType: "text/html", body: `
    <div id="selected"></div><script>
    addEventListener('message', e => { if(e.source === parent && e.data.type === 'design-system:select-story') document.querySelector('#selected').textContent = e.data.story_id; });
    parent.postMessage({ type: 'protoagent:ready' }, '*');
    </script>` }));
  const card = await send(page);
  await expect(card).toContainText("Live Storybook preview");
  await card.getByLabel("Component variant").selectOption("button--ghost");
  await expect(card.locator("iframe")).toHaveAttribute("src", /id=button--ghost/);
  await card.getByRole("button", { name: "Open playground" }).click();
  await expect(page.frameLocator('iframe[src*="/plugins/design-system/view"]').locator("#selected")).toHaveText("button--ghost");
});

test("an unavailable saved preview does not break the transcript", async ({ page }) => {
  await page.route("**/api/plugins/design-system/previews/*", route => route.fulfill({ status: 404, json: { detail: "gone" } }));
  const card = await send(page);
  await expect(card).toContainText("Preview unavailable");
  await expect(page.getByText("Here is the design preview.")).toBeVisible();
});
