import { expect, test } from "@playwright/test";

// Memory inspector (ADR 0069 D7): the rail surface auditing the memory DELIVERY
// layer — session-summary digest rows, hot memory, and the per-turn injection
// record — with delete flows that confirm + toast.

// The delete tests MUTATE the shared mock (a session row / hot chunk is removed)
// and several tests flip the mock's degradation mode (500s / store-off / legacy
// backend), so run serially and reset the memory fixtures + mode before each
// test — the same hermeticity guard the knowledge spec uses.
test.describe.configure({ mode: "serial" });

// Fresh page → Memory surface. Tests that pre-set a mock mode re-run this so the
// first fetch happens under that mode (a page load resets the query cache). The
// shell PERSISTS the open surface across reloads (uiStore hydrates synchronously
// from localStorage, so a restored surface mounts in the SAME commit that paints
// the rail) — clicking the rail button then would TOGGLE it closed. Once the rail
// is visible, one instant check decides restored vs. fresh; no timeout probe.
async function openMemory(page: import("@playwright/test").Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const surface = page.getByTestId("memory-surface");
  // exact: role-name matching is substring by default, and once the surface is
  // restored a session row whose topic mentions "memory" would also match.
  const rail = page.getByRole("button", { name: "Memory", exact: true });
  await expect(rail).toBeVisible();
  if (!(await surface.isVisible())) await rail.click();
  await expect(surface).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/memory/reset");
  await openMemory(page);
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

  // Filtering to one session narrows the table (the input debounces ~250ms
  // before the query refires; the assertions auto-wait through it). The
  // placeholder says the match is EXACT — it's a lookup, not a substring search.
  const filterInput = surface.getByLabel("filter injections by session id");
  await expect(filterInput).toHaveAttribute("placeholder", "Exact session id (blank = all sessions)…");
  await filterInput.fill("sched-hourly-report");
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

// ── Delivery-truth badges (injection window / digest window) ─────────────────

test("rows outside the digest/injection window are badged; true and legacy rows are not", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");

  // Sessions: in_digest:false → badge; in_digest:true and absent (legacy) → none.
  const outRow = surface.locator(".memory-row", { hasText: "sched-hourly-report" });
  await expect(outRow.getByText("not in digest")).toBeVisible();
  const inRow = surface.locator(".memory-row", { hasText: "chat-1750000000000-abc123" });
  await expect(inRow.getByText("not in digest")).toHaveCount(0);
  const legacyRow = surface.locator(".memory-row", { hasText: "a2a-legacy-noflags" });
  await expect(legacyRow.getByText("not in digest")).toHaveCount(0);

  // size_bytes renders human-readable in the meta line; the legacy row omits it.
  await expect(inRow.getByText(/2\.0 KB/)).toBeVisible();
  await expect(outRow.getByText(/512 B/)).toBeVisible();
  await expect(legacyRow.getByText(/ B\b|KB/)).toHaveCount(0);

  // Hot memory: injecting:false → badge; injecting:true and absent → none.
  await surface.getByRole("tab", { name: "Hot memory" }).click();
  const hotOut = surface.locator(".memory-row", { hasText: "Weekly report goes out" });
  await expect(hotOut.getByText("not injecting")).toBeVisible();
  const hotIn = surface.locator(".memory-row", { hasText: "US/Pacific" });
  await expect(hotIn.getByText("not injecting")).toHaveCount(0);
  const hotLegacy = surface.locator(".memory-row", { hasText: "Legacy row predating" });
  await expect(hotLegacy.getByText("not injecting")).toHaveCount(0);
});

test("a backend without the delivery fields renders exactly as before — no badges, no sizes", async ({ page }) => {
  // Graceful degradation is a contract: a backend predating injecting/in_digest/
  // size_bytes (or a custom store) must render the pre-badge view, not crash.
  await page.request.post("/api/__test__/memory/mode", { data: { legacy: true } });
  await openMemory(page);
  const surface = page.getByTestId("memory-surface");

  await expect(surface.getByText("chat-1750000000000-abc123")).toBeVisible();
  await expect(surface.getByText("not in digest")).toHaveCount(0);
  await expect(surface.getByText(/KB/)).toHaveCount(0);

  await surface.getByRole("tab", { name: "Hot memory" }).click();
  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByText("not injecting")).toHaveCount(0);
});

// ── Error / empty / store-off branches ───────────────────────────────────────

test("each tab shows its contained error alert when the backend read fails", async ({ page }) => {
  await page.request.post("/api/__test__/memory/mode", { data: { fail: "sessions" } });
  await openMemory(page);
  const surface = page.getByTestId("memory-surface");
  await expect(surface.getByText(/Couldn't list session summaries/)).toBeVisible();

  await page.request.post("/api/__test__/memory/mode", { data: { fail: "hot" } });
  await surface.getByRole("tab", { name: "Hot memory" }).click();
  await expect(surface.getByText(/Couldn't list hot memory/)).toBeVisible();

  await page.request.post("/api/__test__/memory/mode", { data: { fail: "injections" } });
  await surface.getByRole("tab", { name: "Injections" }).click();
  await expect(surface.getByText(/Couldn't read the injection log/)).toBeVisible();
});

test("empty stores render the Empty states, not errors", async ({ page }) => {
  await page.request.post("/api/__test__/memory/mode", { data: { empty: true } });
  await openMemory(page);
  const surface = page.getByTestId("memory-surface");

  await expect(surface.getByText("No session summaries")).toBeVisible();
  await surface.getByRole("tab", { name: "Hot memory" }).click();
  await expect(surface.getByText("No hot memory")).toBeVisible();
  await surface.getByRole("tab", { name: "Injections" }).click();
  await expect(surface.getByText("No injection records")).toBeVisible();
});

test("a disabled knowledge store shows the store-off notice on the hot tab", async ({ page }) => {
  await page.request.post("/api/__test__/memory/mode", { data: { enabled: false } });
  await openMemory(page);
  const surface = page.getByTestId("memory-surface");

  await surface.getByRole("tab", { name: "Hot memory" }).click();
  await expect(surface.getByText(/knowledge store is off/)).toBeVisible();
});

// ── Cancel paths + the replaced:false warning ────────────────────────────────

test("cancel paths leave rows untouched: both delete dialogs dismiss, edit Cancel reverts", async ({ page }) => {
  const surface = page.getByTestId("memory-surface");

  // Session delete → Cancel: dialog closes, row survives.
  await surface.getByLabel("delete session sched-hourly-report").click();
  const dialog = page.getByRole("dialog", { name: "Delete this session summary?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(surface.getByText("sched-hourly-report")).toBeVisible();

  // Hot edit → Cancel: the draft is discarded, the row shows the original text.
  await surface.getByRole("tab", { name: "Hot memory" }).click();
  await surface.getByLabel("edit hot entry 31").click();
  const field = surface.getByLabel("hot entry 31 content");
  await field.fill("scribble that must not persist");
  await surface.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByText("scribble that must not persist")).toHaveCount(0);

  // Hot delete → Cancel: dialog closes, chunk survives.
  await surface.getByLabel("delete hot entry 32").click();
  const hotDialog = page.getByRole("dialog", { name: "Delete this hot-memory entry?" });
  await expect(hotDialog).toBeVisible();
  await hotDialog.getByRole("button", { name: "Cancel" }).click();
  await expect(hotDialog).not.toBeVisible();
  await expect(surface.getByText("Weekly report goes out Fridays at 9am.")).toBeVisible();
});

test("a hot edit whose old revision couldn't be removed warns via toast", async ({ page }) => {
  await page.request.post("/api/__test__/memory/mode", { data: { replaced: false } });
  const surface = page.getByTestId("memory-surface");
  await surface.getByRole("tab", { name: "Hot memory" }).click();

  await surface.getByLabel("edit hot entry 31").click();
  await surface.getByLabel("hot entry 31 content").fill("The operator works in US/Eastern.");
  await surface.getByRole("button", { name: "Save", exact: true }).click();

  // Warning tone, not success — both revisions may inject until one is pruned.
  await expect(
    page.locator(".pl-toast.pl-toast--warning", { hasText: "both may inject" }),
  ).toBeVisible();
});
