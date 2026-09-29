import { expect, test } from "@playwright/test";

// Cross-agent turn-finished notifications: a window watches the OTHER slugs'
// persisted in-flight turns (localStorage) and polls their durable tasks via the
// hub proxy; a terminal task surfaces as a toast. The mock's tasks/get always
// answers `completed`, so a seeded "streaming" turn on another slug resolves on
// the watcher's first round.

test("a finished turn on another agent raises a toast in this window", async ({ page }) => {
  await page.addInitScript(() => {
    const session = {
      version: 1,
      currentSessionId: "chat-x",
      sessions: [{
        id: "chat-x",
        title: "Summarize the quarterly numbers",
        createdAt: 1, updatedAt: 2,
        messages: [
          { id: "u1", role: "user", content: "summarize", status: "done" },
          { id: "a1", role: "assistant", content: "working…", status: "streaming", taskId: "task-bg-1" },
        ],
      }],
    };
    // Another agent's window persisted this (slug `roxy`); we open the HOST window.
    window.localStorage.setItem("protoagent.chat.sessions:roxy", JSON.stringify(session));
  });
  await page.goto("/app/", { waitUntil: "load" });

  // The watcher's first round polls /agents/roxy/a2a tasks/get → completed → toast,
  // with the agent's display name from /api/fleet.
  await expect(page.getByText("roxy finished a turn")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/Summarize the quarterly numbers/)).toBeVisible();
});

test("the focused agent's own turns do not toast", async ({ page }) => {
  await page.addInitScript(() => {
    const own = {
      version: 1,
      currentSessionId: "chat-y",
      sessions: [{
        id: "chat-y", title: "Own work", createdAt: 1, updatedAt: 2,
        messages: [{ id: "a2", role: "assistant", content: "…", status: "streaming", taskId: "task-self-1" }],
      }],
    };
    window.localStorage.setItem("protoagent.chat.sessions", JSON.stringify(own)); // the HOST's own key
    // A second agent's in-flight turn: its toast is the proof a watcher round really ran, in
    // place of a fixed sleep "long enough for a round to (wrongly) fire".
    const other = {
      version: 1,
      currentSessionId: "chat-z",
      sessions: [{
        id: "chat-z", title: "Other work", createdAt: 1, updatedAt: 2,
        messages: [{ id: "a3", role: "assistant", content: "…", status: "streaming", taskId: "task-bg-2" }],
      }],
    };
    window.localStorage.setItem("protoagent.chat.sessions:roxy", JSON.stringify(other));
  });
  // The watcher's GetTask carries `watch-<taskId>` as its JSON-RPC id — distinct from the
  // chat slot's own reattach of the same task, which is expected and not a watch.
  const watched: string[] = [];
  page.on("request", (r) => {
    const body = r.postData() ?? "";
    const m = /"id":"watch-([^"]+)"/.exec(body);
    if (m) watched.push(m[1]);
  });
  await page.goto("/app/", { waitUntil: "load" });
  // One round polls every candidate in turn, and a toast only follows an `await` on the
  // fleet name lookup — so by the time roxy's toast shows, a watch of our own task (had
  // the round made one) has already gone out.
  await expect(page.getByText("roxy finished a turn")).toBeVisible({ timeout: 10_000 });
  expect(watched).toContain("task-bg-2");
  expect(watched, "this window's own turn streams live — it is never watched").not.toContain("task-self-1");
  await expect(page.getByText(/finished a turn/)).toHaveCount(1);
});
