import { expect, test } from "@playwright/test";
import { seedCurrentChat } from "./chat-helpers";

test("assistant action drafts, refuses an empty case, then saves an expectation", async ({ page }) => {
  let sessionId = "";
  const writes: Record<string, unknown>[] = [];
  await page.route("**/api/feedback**", async (route) => {
    sessionId = new URL(route.request().url()).searchParams.get("session_id")!;
    await route.fulfill({ json: { enabled: true, tracing_enabled: true, turns: [
      { task_id: "task-e2e-1", trace_id: "eval-trace", created_at: "", rating: 0, note: "Needs sources" },
    ] } });
  });
  await page.route("**/api/evals/cases/draft", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ session_id: sessionId, trace_id: "eval-trace" });
    await route.fulfill({ json: { id: "eval-case", name: "Promoted turn", category: "instance", kind: "ask", prompt: "Find sources", source_trace_id: "eval-trace", tier: "gate", comment: "Needs sources" } });
  });
  await page.route("**/api/evals/cases", async (route) => {
    expect(route.request().method()).toBe("POST");
    const body = route.request().postDataJSON().case;
    writes.push(body);
    await route.fulfill(body.expected_patterns.length
      ? { status: 201, json: { ok: true, case: body } }
      : { status: 422, json: { detail: "Case must assert something" } });
  });
  await page.goto("/app/"); await seedCurrentChat(page, "Find sources");
  await page.getByRole("button", { name: "Make eval case", exact: true }).first().click();
  await expect(page.getByRole("textbox", { name: "Prompt", exact: true })).toHaveValue("Find sources");
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Case must assert something" })).toBeVisible();
  expect(writes[0]).not.toHaveProperty("expected_tools");
  await page.getByRole("textbox", { name: "Expected reply patterns", exact: true }).fill("https://example.com");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Prompt", exact: true })).toHaveCount(0);
  expect(writes).toHaveLength(2);
  expect(writes[1]).toMatchObject({ expected_patterns: ["https://example.com"], source_trace_id: "eval-trace", tier: "gate" });
});
