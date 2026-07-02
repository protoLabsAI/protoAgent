import { expect, test } from "@playwright/test";

// Composer Stop (#1617). While a turn streams, the Stop control must actually
// halt it: a server-side A2A CancelTask on the wire — the only thing that stops
// the turn when the local stream can't be aborted (the desktop relay ignores the
// abort signal; a re-attached slot has no controller at all) — plus the thread
// settled locally so no bubble is left `streaming`. Before the fix, Stop was a
// silent no-op whenever the slot's live taskId state was empty.

test("Stop during a streaming turn sends CancelTask for the turn's task and settles the thread", async ({
  page,
}) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();

  // Start a turn the mock HOLDS OPEN — the surface stays "streaming", the state
  // where the kit renders the dedicated Stop control beside Send.
  await composer.fill("hold the turn open");
  await composer.press("Enter");
  const stop = page.getByRole("button", { name: "Stop" });
  await expect(stop).toBeVisible();

  // Stop must put a CancelTask for THIS turn's task id on the wire — prove the
  // RPC, not just the local settle (the server cancel is what halts generation).
  const cancelled = page.waitForRequest(
    (r) =>
      r.method() === "POST" &&
      r.url().endsWith("/a2a") &&
      (r.postData() || "").includes('"CancelTask"'),
  );
  await stop.click();
  const req = await cancelled;
  expect(req.postData()).toContain("task-e2e-1");

  // Locally settled: composer back to idle (Stop gone, send placeholder back),
  // and no assistant bubble left spinning.
  await expect(stop).toBeHidden();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
});
