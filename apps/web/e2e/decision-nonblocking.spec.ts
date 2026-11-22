import { expect, test } from "@playwright/test";
import type { DecisionRow } from "../src/lib/api/decisions";

test("a finished turn's decision stays pending while ordinary chat continues", async ({ page }) => {
  let row: DecisionRow = {
    id: "nonblocking", session_id: "", agent: "protoEngineer", question: "Ship the release?",
    options: [{ id: "ship", label: "Ship the release" }],
    created_at: "2026-11-09T14:00:00Z", state: "pending", answer: null, answered_at: null,
    expires_at: null, default_option: null, thread_id: "", interrupt_id: "",
    context: "", default_action: null, withdraw_reason: null, answered_by: null,
    answered_surface: null, default_action_text: "keep holding", answer_label: null,
  };
  await page.route("**/api/decisions?**", (route) => route.fulfill({ json: {
    decisions: row.state === "pending" ? [row] : [], pending: row.state === "pending" ? 1 : 0,
  } }));
  await page.route("**/api/decisions/nonblocking", (route) => route.fulfill({ json: row }));
  await page.route("**/api/decisions/nonblocking/answer", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ option_id: "ship", surface: "console" });
    row = { ...row, state: "answered", answer: "ship", answer_label: "Ship the release",
      answered_at: new Date().toISOString() };
    await route.fulfill({ json: { decision: row, resumed: false } });
  });
  let turns = 0;
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON();
    const message = body.params?.message;
    if (!message) return route.continue();
    expect(message.metadata ?? {}).not.toHaveProperty("hitl_resume");
    row.session_id = message.contextId;
    const taskId = `nonblocking-${++turns}`;
    const parts = turns === 1 ? [{ data: { kind: "decision", decision_id: row.id },
      metadata: { mimeType: "application/vnd.protolabs.hitl-v1+json" } }] : [{ text: "Here is the unrelated reply." }];
    const frames = [
      { artifactUpdate: { taskId, artifact: { parts } } },
      { statusUpdate: { taskId, status: { state: "completed" }, final: true } },
    ];
    await route.fulfill({ contentType: "text/event-stream",
      body: frames.map((result) => `data: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result })}\n\n`).join("") });
  });
  await page.goto("/app/");
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("Ask me whether to ship");
  await composer.press("Enter");
  const card = page.locator('.chat-session-slot:not([hidden]) .decision-transcript');
  await expect(card).toContainText("Ship the release?");
  await composer.fill("An unrelated question");
  await composer.press("Enter");
  await expect(page.locator('.chat-session-slot:not([hidden])')).toContainText("Here is the unrelated reply.");
  expect(turns).toBe(2);
  expect(row.state).toBe("pending");
  await expect(card.getByRole("button", { name: "Ship the release", exact: true })).toBeEnabled();
  await card.getByRole("button", { name: "Ship the release", exact: true }).click();
  await expect(card).toContainText('Sent: "Ship the release"');
  expect(row.state).toBe("answered");
});
