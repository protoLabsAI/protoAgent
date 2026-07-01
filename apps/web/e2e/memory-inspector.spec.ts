import { expect, test } from "@playwright/test";

// Memory inspector (ADR 0069 D7): the rail surface auditing the memory DELIVERY
// layer — session-summary digest rows, hot memory, and the per-turn injection
// record — with delete flows that confirm + toast.

// The delete tests MUTATE the shared mock (a session row / hot chunk is removed),
// so run serially and reset the memory fixtures before each test — the same
// hermeticity guard the knowledge spec uses.
test.describe.configure({ mode: "serial" });
test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/memory/reset");
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Memory" }).click();
  await expect(page.getByTestId("memory-surface")).toBeVisible();
});

test("Sessions panel lists digest rows and opens the full summary in the reader", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");

  // Digest-derived rows: id, surface badge, topic, message count.
  await expect(surface.getByText("chat-1750000000000-abc123")).toBeVisible();
  await expect(surface.getByText("plan the memory hardening rollout")).toBeVisible();
  await expect(surface.getByText("background", { exact: true })).toBeVisible();
  await expect(surface.getByText(/12 msgs/)).toBeVisible();

  // Row click → the document viewer shows the FULL rendered summary (verbatim pre).
  await surface.getByText("plan the memory hardening rollout").click();
  const pre = page.locator(".memory-session-pre");
  await expect(pre).toBeVisible();
  await expect(pre).toContainText('<session id="chat-1750000000000-abc123"');
  await expect(pre).toContainText("<user>plan the memory hardening rollout</user>");
});

test("deleting a session summary confirms, toasts, and drops the row", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");

  await surface.getByLabel("delete session sched-hourly-report").click();
  const dialog = page.getByRole("dialog", { name: "Delete this session summary?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete summary" }).click();

  // Transient result feedback rides a DS toast (console convention).
  await expect(page.locator(".pl-toast", { hasText: "Session summary sched-hourly-report deleted." })).toBeVisible();
  await expect(surface.getByText("sched-hourly-report")).toHaveCount(0);
});

test("Hot memory panel lists always-on chunks; delete confirms and toasts", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");
  await surface.getByRole("tab", { name: "Hot memory" }).click();

  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByText("Weekly report goes out Fridays at 9am.")).toBeVisible();
  // Provenance badge (ADR 0069 D5): the source session that wrote the row.
  await expect(surface.getByText("chat-1750000000000-abc123")).toBeVisible();

  await surface.getByLabel("delete hot entry 32").click();
  const dialog = page.getByRole("dialog", { name: "Delete this hot-memory entry?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete entry" }).click();

  await expect(page.locator(".pl-toast", { hasText: "Hot-memory entry deleted." })).toBeVisible();
  await expect(surface.getByText("Weekly report goes out Fridays at 9am.")).toHaveCount(0);
});

test("editing a hot-memory entry saves via PUT, toasts, and shows the revision", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");
  await surface.getByRole("tab", { name: "Hot memory" }).click();

  await surface.getByLabel("edit hot entry 31").click();
  const field = surface.getByLabel("hot entry 31 content");
  await expect(field).toHaveValue("The operator works in US/Pacific.");
  await field.fill("The operator works in US/Eastern.");
  await surface.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.locator(".pl-toast", { hasText: "Hot-memory entry updated." })).toBeVisible();
  // The list refetches and shows the new revision (the backend re-adds + deletes,
  // pinning domain="hot" — the row content is what must survive).
  await expect(surface.getByText("The operator works in US/Eastern.")).toBeVisible();
  await expect(surface.getByText("The operator works in US/Pacific.")).toHaveCount(0);
});

test("Injections panel shows the per-turn record and filters by session id", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");
  await surface.getByRole("tab", { name: "Injections" }).click();

  // Both fixture rows, with their injected ids visible.
  const table = surface.locator(".memory-injections");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(table.getByText("31, 32")).toBeVisible(); // hot chunk ids
  await expect(table.getByText("sched-hourly-report").first()).toBeVisible();

  // Filtering to one session narrows the table.
  await surface.getByLabel("filter injections by session id").fill("sched-hourly-report");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByText("chat-1750000000000-abc123")).toHaveCount(0);
});

test("a session row's injections jump pre-filters the Injections panel", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");
  await surface.getByLabel("injections for chat-1750000000000-abc123").click();

  // Landed on the Injections tab, filter applied, only that session's rows shown.
  await expect(surface.getByLabel("filter injections by session id")).toHaveValue(
    "chat-1750000000000-abc123",
  );
  await expect(surface.locator(".memory-injections tbody tr")).toHaveCount(1);
});
