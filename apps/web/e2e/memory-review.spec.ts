import { expect, test } from "@playwright/test";

// Review pass (ADR 0125 P3 D6/D7): the Memory inspector's Review tab — the operator's
// one-time curation of the migrated core memories (keep / edit / forget / reject / revert)
// plus explicit shared-profile promotion. Nothing promotes automatically.
//
// These tests MUTATE the shared mock (verdicts, edits, deletes, a revert, a promote) and one
// flips the mock's shared-profile switch, so run serially and reset the memory fixtures +
// mode before each test — the same hermeticity guard the other memory specs use.
test.describe.configure({ mode: "serial" });

async function openMemory(page: import("@playwright/test").Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const surface = page.getByTestId("memory-surface");
  const rail = page.getByRole("button", { name: "Memory", exact: true });
  await expect(rail).toBeVisible();
  if (!(await surface.isVisible())) await rail.click();
  await expect(surface).toBeVisible();
  return surface;
}

async function openReview(page: import("@playwright/test").Page) {
  const surface = await openMemory(page);
  await surface.getByRole("tab", { name: "Review" }).click();
  return surface;
}

test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/memory/reset");
});

test("lists pending migrated rows by default, and the filters change the request", async ({ page }) => {
  const surface = await openMemory(page);

  // The mount request carries the default review_state=pending (the migration queue).
  const firstReq = page.waitForRequest((r) => /\/api\/memory\/memories\?/.test(r.url()));
  await surface.getByRole("tab", { name: "Review" }).click();
  expect(new URL((await firstReq).url()).searchParams.get("review_state")).toBe("pending");

  // Default rows: the pending migrated memories (201 profile, 202 fact). A confirmed /
  // rejected / superseded row is filtered out by the default pending-only view.
  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByText("The staging deploy runs at 2am.")).toBeVisible();
  await expect(surface.getByText("from preferences")).toBeVisible();
  await expect(surface.getByText("We chose SQLite over Postgres.")).toHaveCount(0);

  // A kind chip narrows the request (kind=profile) and the list.
  const kindReq = page.waitForRequest((r) => /\/api\/memory\/memories\?.*kind=profile/.test(r.url()));
  await surface.getByRole("button", { name: "Kind: profile" }).click();
  await kindReq;
  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByText("The staging deploy runs at 2am.")).toHaveCount(0);

  // A review_state chip swaps the queue: the rejected row appears.
  const stateReq = page.waitForRequest((r) => /review_state=rejected/.test(r.url()));
  await surface.getByRole("button", { name: "Kind: All kinds" }).click();
  await surface.getByRole("button", { name: "State: rejected" }).click();
  await stateReq;
  await expect(surface.getByText("We chose SQLite over Postgres.")).toBeVisible();
});

test("Keep confirms a row and it leaves the pending queue", async ({ page }) => {
  const surface = await openReview(page);
  const post = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/api/memory/memories/202/review"),
  );
  await surface.getByLabel("keep memory 202").click();
  expect((await post).postDataJSON()).toEqual({ state: "confirmed" });

  await expect(page.locator(".pl-toast", { hasText: "Kept." })).toBeVisible();
  // Confirmed → no longer pending → drops out of the default queue after the refetch.
  await expect(surface.getByText("The staging deploy runs at 2am.")).toHaveCount(0);
});

test("Reject rejects a row and it leaves the pending queue", async ({ page }) => {
  const surface = await openReview(page);
  const post = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/api/memory/memories/201/review"),
  );
  await surface.getByLabel("reject memory 201").click();
  expect((await post).postDataJSON()).toEqual({ state: "rejected" });

  await expect(page.locator(".pl-toast", { hasText: "Rejected." })).toBeVisible();
  await expect(surface.getByText("The operator works in US/Pacific.")).toHaveCount(0);
});

test("Edit changes subject and kind via PATCH and the list updates", async ({ page }) => {
  const surface = await openReview(page);
  // Show all states so the edited row (which confirms) stays in view.
  await surface.getByRole("button", { name: "State: All states" }).click();

  await surface.getByLabel("edit memory 201").click();
  await surface.getByLabel("memory 201 content").fill("The operator works in US/Eastern.");
  await surface.getByLabel("memory 201 subject").fill("tz");
  await surface.getByLabel("memory 201 kind").selectOption("standing");

  const patch = page.waitForRequest(
    (r) => r.method() === "PATCH" && r.url().endsWith("/api/memory/memories/201"),
  );
  await surface.getByRole("button", { name: "Save", exact: true }).click();
  const body = (await patch).postDataJSON();
  expect(body.subject).toBe("tz");
  expect(body.kind).toBe("standing");

  await expect(page.locator(".pl-toast", { hasText: "Memory updated." })).toBeVisible();
  await expect(surface.getByText("The operator works in US/Eastern.")).toBeVisible();
  await expect(surface.getByText("The operator works in US/Pacific.")).toHaveCount(0);
});

test("Forget removes the row after the confirm dialog", async ({ page }) => {
  const surface = await openReview(page);
  await surface.getByLabel("forget memory 202").click();
  const dialog = page.getByRole("dialog", { name: "Forget this memory?" });
  await expect(dialog).toBeVisible();
  const del = page.waitForRequest(
    (r) => r.method() === "DELETE" && r.url().endsWith("/api/memory/hot/memory:202"),
  );
  await dialog.getByRole("button", { name: "Forget" }).click();
  await del;

  await expect(page.locator(".pl-toast", { hasText: "Memory forgotten." })).toBeVisible();
  await expect(surface.getByText("The staging deploy runs at 2am.")).toHaveCount(0);
});

test("Revert undoes a supersession on a superseded row", async ({ page }) => {
  const surface = await openReview(page);
  // A superseded row is hidden until the Superseded chip brings it into view.
  await surface.getByRole("button", { name: "include superseded" }).click();
  const row = surface.locator(".memory-row", { hasText: "An old note that got consolidated." });
  await expect(row).toBeVisible();
  await expect(row.getByText("superseded")).toBeVisible();

  const post = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/api/memory/memories/204/revert"),
  );
  await surface.getByLabel("revert memory 204").click();
  await post;

  await expect(page.locator(".pl-toast", { hasText: "Supersession reverted." })).toBeVisible();
  // Live again: the superseded badge + Revert button are gone on the refetched row.
  await expect(surface.getByLabel("revert memory 204")).toHaveCount(0);
});

test("promotes a candidate once; an already-shared candidate is disabled", async ({ page }) => {
  const surface = await openReview(page);
  const candidates = surface.getByTestId("profile-candidates");
  await expect(candidates).toBeVisible();

  // 302 is already in the shared profile — a disabled "Shared", never a Promote.
  const shared = candidates.getByLabel("shared 302");
  await expect(shared).toBeVisible();
  await expect(shared).toBeDisabled();

  // 301 promotes once; the button then becomes the disabled "Shared".
  const post = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/api/memory/memories/301/promote"),
  );
  await candidates.getByLabel("promote 301").click();
  await post;
  await expect(page.locator(".pl-toast", { hasText: "Promoted to the shared profile." })).toBeVisible();
  await expect(candidates.getByLabel("promote 301")).toHaveCount(0);
  await expect(candidates.getByLabel("shared 301")).toBeDisabled();
});

test("the candidates section is hidden when the shared profile is off", async ({ page }) => {
  await page.request.post("/api/__test__/memory/mode", { data: { sharedProfile: false } });
  const surface = await openReview(page);

  // The review list still works; only the candidates section disappears.
  await expect(surface.getByText("The operator works in US/Pacific.")).toBeVisible();
  await expect(surface.getByTestId("profile-candidates")).toHaveCount(0);
});

test("Forget uses the core route for retrieved memories", async ({ page }) => {
  const surface = await openReview(page);
  await surface.getByLabel("forget memory 201").click();
  const response = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && r.url().endsWith("/api/memory/memories/201"),
  );
  await page.getByRole("dialog").getByRole("button", { name: "Forget" }).click();
  expect((await response).ok()).toBe(true);
  await expect(surface.getByLabel("forget memory 201")).toHaveCount(0);
});

for (const count of [51, 101]) {
  for (const action of ["keep", "reject", "forget"]) {
    test(`${action} on the last of ${count} rows returns to the remaining queue`, async ({ page }) => {
      const initial = await page.request.get("/api/memory/memories");
      const template = (await initial.json()).memories[0];
      const rows = Array.from({ length: count }, (_, i) => ({
        ...template, id: 1000 + i, content: `Review item ${i}`,
      }));
      await page.request.post("/api/__test__/memory/review", { data: { rows } });
      const surface = await openReview(page);
      for (let i = 0; i < Math.floor(count / 50); i++) {
        await surface.getByRole("button", { name: "Next", exact: true }).click();
        await expect(surface.locator(".memory-paging")).toContainText(
          `${(i + 1) * 50 + 1}–${Math.min((i + 2) * 50, count)} of ${count}`,
        );
      }
      await surface.getByLabel(`${action} memory ${999 + count}`).click();
      if (action === "forget") {
        await page.getByRole("dialog").getByRole("button", { name: "Forget" }).click();
      }
      await expect(surface.getByLabel(`keep memory ${998 + count}`)).toBeVisible();
      await expect(surface.getByText("Nothing to review", { exact: true })).toHaveCount(0);
      await expect(surface.getByLabel(`${action} memory ${999 + count}`)).toHaveCount(0);
      if (count === 101) {
        await expect(surface.locator(".memory-paging")).toContainText("51–100 of 100");
        await surface.getByRole("button", { name: "Previous", exact: true }).click();
        await expect(surface.getByLabel("keep memory 1000")).toBeVisible();
      }
    });
  }
}

test("a promotion conflict refreshes the candidate", async ({ page }) => {
  const surface = await openReview(page);
  await expect(surface.getByLabel("promote 301")).toBeVisible();
  await page.request.post("/api/memory/memories/301/promote");
  const response = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/301/promote"),
  );
  await surface.getByLabel("promote 301").click();
  expect((await response).status()).toBe(409);
  await expect(surface.getByLabel("shared 301")).toBeDisabled();
});

for (const [mode, message] of [
  [{ fail: "review" }, "Couldn't list memories"],
  [{ empty: true }, "Nothing to review"],
  [{ enabled: false }, "The memory store is off"],
] as const) {
  test(`review state: ${message}`, async ({ page }) => {
    await page.request.post("/api/__test__/memory/mode", { data: mode });
    const surface = await openReview(page);
    await expect(surface.getByText(message, { exact: false })).toBeVisible();
  });
}

test("Review screenshots at desktop and phone widths", async ({ page }, testInfo) => {
  const surface = await openReview(page);
  await expect(surface.getByLabel("keep memory 202")).toBeVisible();
  for (const width of [1200, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const path = testInfo.outputPath(`review-${width}.png`);
    await page.screenshot({ path, fullPage: true });
    await testInfo.attach(`review-${width}`, { path, contentType: "image/png" });
  }
});
