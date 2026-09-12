import { expect, test } from "@playwright/test";

import { toolCardsSettled } from "./toolcard";

// A single-tool turn: the operator expands the RUNNING card (the live spotlight), the turn
// settles, and the card they opened must still be the card on screen — same DOM node, still
// expanded. It used to be torn down and rebuilt at the settle (the live spotlight and the
// settled inline card were different trees), and the DS ToolCard's uncontrolled `open` meant
// the rebuilt card always came back collapsed. Found in QA of v0.164.0 on the desktop app.
//
// The guard is NODE IDENTITY across the transition (element handles), not "is it open": a
// remount can default open and pass the latter. The turn is PARKED mid-tool by the mock
// ("PARK THE TOOL") and released by this spec, so the expand is guaranteed to land while the
// card is live and the settle happens after it — no race against machine speed.
test("an expanded single-tool card survives the turn settling — same node, still open", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  // The session id rides the stream request (A2A `contextId`); the park is keyed by it, so
  // this spec can only ever release its own turn on the shared mock.
  const streamRequest = page.waitForRequest(
    (r) => r.url().endsWith("/a2a") && r.method() === "POST" && r.postDataJSON()?.method === "SendStreamingMessage",
  );
  await composer.fill("PARK THE TOOL mid-run");
  await composer.press("Enter");
  const sessionId = String((await streamRequest).postDataJSON().params.message.contextId);

  // Live: the lone tool is RUNNING in the spotlight slot. Expand it.
  const liveCard = page.locator(".tool-spotlight .pl-toolcard");
  await expect(liveCard).toHaveClass(/pl-toolcard--running/);
  await liveCard.locator(".pl-toolcard__head").click();
  await expect(liveCard.locator(".pl-toolcard__head")).toHaveAttribute("aria-expanded", "true");
  const expanded = await liveCard.elementHandle();
  expect(expanded).not.toBeNull();

  // Release the turn: tool end → answer → terminal frame, i.e. an ordinary settle.
  const release = await page.request.post(`/api/__test__/turns/${encodeURIComponent(sessionId)}/release`);
  expect((await release.json()).released).toBe(true);

  await toolCardsSettled(page);
  await expect(page.getByText("Done — found 8 results.")).toBeVisible();
  const settledCard = page.locator(".pl-toolcard");
  await expect(settledCard).toHaveCount(1);
  await expect(settledCard).toHaveClass(/pl-toolcard--done/);

  // THE guard: the settled card IS the node the operator expanded (a remount detaches it).
  expect(await expanded!.evaluate((el) => el.isConnected)).toBe(true);
  expect(await settledCard.evaluate((el, prior) => el === prior, expanded)).toBe(true);
  // …which is why the expansion survived, now showing the result that arrived with the settle.
  await expect(settledCard.locator(".pl-toolcard__head")).toHaveAttribute("aria-expanded", "true");
  await expect(settledCard.locator(".pl-toolcard__body")).toContainText("First Result");
});
