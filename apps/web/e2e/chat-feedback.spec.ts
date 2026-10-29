import { expect, test } from "@playwright/test";
import { seedCurrentChat } from "./chat-helpers";

test("thumbs-down note survives reload via session feedback", async ({ page }) => {
  let rating: number | null = null;
  let note: string | null = null;
  let sessionId = "";
  let reads = 0;
  await page.route("**/api/feedback**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      expect(body.session_id).toBe(sessionId);
      expect(body.trace_id).toBe("feedback-trace");
      expect(body.incognito).toBe(false);
      rating = body.value;
      note = body.note;
      await route.fulfill({ json: { ok: true, rating, note } });
    } else {
      reads++;
      sessionId = new URL(request.url()).searchParams.get("session_id")!;
      await route.fulfill({ json: { enabled: true, tracing_enabled: true, turns: [
        { task_id: "task-e2e-1", trace_id: "feedback-trace", created_at: "", rating, note },
      ] } });
    }
  });
  await page.goto("/app/");
  await seedCurrentChat(page, "Feedback test");
  const down = page.getByRole("button", { name: "Thumbs down", exact: true }).first();
  await down.click();
  await expect(down).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("textbox", { name: "Feedback note (optional)" }).fill("Please cite sources");
  await page.getByRole("button", { name: "Save note", exact: true }).click();
  await expect(page.getByText("Please cite sources", { exact: true })).toBeVisible();
  expect(note).toBe("Please cite sources");
  reads = 0;
  await page.reload();
  await expect(down).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Please cite sources", { exact: true })).toBeVisible();
  expect(reads).toBe(1);
});
