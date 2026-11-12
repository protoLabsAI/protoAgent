// Capture the Work four-group evidence set (work-consolidation.md v2) from the BUILT app
// against the e2e mock server — 1440 and 390, light and dark: mixed-group Overview, the
// window inspector detail, a picked-empty group, and the phone pushed detail.
//
//   npm run build && node e2e/mock-server.mjs 4621 &
//   node scripts/capture-work-groups.mjs http://127.0.0.1:4621 e2e/screenshots/work-groups
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const BASE = process.argv[2] ?? "http://127.0.0.1:4621";
const OUT = process.argv[3] ?? "e2e/screenshots/work-groups";

const DECISION = {
  id: "shot-decision", agent: "protoAgent", session_id: "", question: "Ship the Work view?",
  options: [{ id: "yes", label: "Ship it" }, { id: "hold", label: "Keep holding" }],
  created_at: new Date(Date.now() - 4 * 60_000).toISOString(), state: "pending", answer: null,
  answered_at: null, expires_at: null, default_option: null, thread_id: "", interrupt_id: "",
  context: "", default_action_text: "Wait", answer_label: null,
};
const DIRECTIVE = {
  id: "shot-directive", session_id: "", quote: "Don't merge anything into protoAgent main until I say so.",
  conditions: "", scope: "Hold protoAgent main merges", status: "in_force",
  created_at: new Date(Date.now() - 50 * 60_000).toISOString(), closed_by: null, closed_at: null, close_reason: null,
};

/** The palette route into the Work window, as the e2e specs drive it. */
async function openWorkWindow(page, label = "Open Work") {
  await page.getByTestId("header-menu").waitFor();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill(label);
  await page.getByRole("option", { name: label, exact: true }).click();
  await page.getByRole("dialog", { name: /^Work/ }).waitFor();
}

async function dismissToasts(page) {
  for (const dismiss of await page.getByRole("button", { name: "Dismiss", exact: true }).all()) {
    await dismiss.click().catch(() => {});
  }
}

async function capture(page, name) {
  // Let the slower sources land so no skeleton or mid-refresh callout rides the evidence.
  await page.locator('[aria-busy="true"]').first().waitFor({ state: "hidden", timeout: 10_000 }).catch(() => {});
  await page.getByText("may be out of date").first().waitFor({ state: "hidden", timeout: 10_000 }).catch(() => {});
  await page.waitForTimeout(400);
  await dismissToasts(page);
  await page.screenshot({ path: join(OUT, `${name}.png`) });
  console.log(`captured ${name}`);
}

const browser = await chromium.launch();
await mkdir(OUT, { recursive: true });
for (const mode of ["light", "dark"]) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
  await page.route("**/api/decisions?*", (route) => route.fulfill({ json: { pending: 1, decisions: [DECISION] } }));
  // The DecisionCard re-reads its record by id; a 404 would mark it missing and drop the row.
  await page.route(/\/api\/decisions\/[^/?]+$/, (route) => route.fulfill({ json: { ...DECISION, delivering: false } }));
  // The mock server has no session inventory; a 404 there marks the directives source stale.
  await page.route("**/api/chat/sessions?*", (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route("**/api/chat/sessions/*/directives", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: { directives: [{ ...DIRECTIVE, session_id: route.request().url().split("/").at(-2) }], in_force: 1 } })
    : route.fallback());
  await page.goto(`${BASE}/app/`, { waitUntil: "load" });
  await page.getByPlaceholder(/Message protoAgent/i).waitFor();

  // 1440: the mixed-group Overview and the ≥48rem window inspector.
  await openWorkWindow(page);
  await page.getByRole("heading", { name: /^Needs you, \d+$/ }).waitFor();
  await capture(page, `overview-1440-${mode}`);
  await page.locator(".work-row", { hasText: "Ship the Work view?" }).click();
  await page.getByRole("button", { name: "Close details" }).waitFor();
  await capture(page, `detail-inspector-1440-${mode}`);
  await page.getByRole("button", { name: "Close details" }).click();

  // 1440: a picked-empty group (Needs you with every feeder emptied).
  await page.route("**/api/decisions?*", (route) => route.fulfill({ json: { pending: 0, decisions: [] } }));
  await page.route("**/api/inbox?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.reload({ waitUntil: "load" });
  await openWorkWindow(page, "Work: Decisions");
  await page.getByText("Nothing needs you.").waitFor();
  await capture(page, `picked-empty-1440-${mode}`);
  await context.close();

  // 390 phone: the list page and the pushed detail with Back.
  const phoneContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const phone = await phoneContext.newPage();
  await phone.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
  await phone.route("**/api/decisions?*", (route) => route.fulfill({ json: { pending: 1, decisions: [DECISION] } }));
  await phone.route(/\/api\/decisions\/[^/?]+$/, (route) => route.fulfill({ json: { ...DECISION, delivering: false } }));
  await phone.goto(`${BASE}/app/`, { waitUntil: "load" });
  await phone.getByRole("button", { name: "Open Work", exact: true }).click();
  await phone.getByRole("heading", { name: /^Needs you, \d+$/ }).waitFor();
  await capture(phone, `overview-390-${mode}`);
  await phone.locator(".work-row", { hasText: "Ship the Work view?" }).click();
  await phone.getByRole("button", { name: /^Back to / }).waitFor();
  await capture(phone, `detail-pushed-390-${mode}`);
  await phoneContext.close();
}
await browser.close();
