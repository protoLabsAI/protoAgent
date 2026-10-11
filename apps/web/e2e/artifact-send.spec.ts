import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Send-to-chat bridge (ADR 0118 D4 / S10b, amended by S19 / #4137): an inline artifact frame may
// call `window.protoArtifact.send(text)` to PROPOSE `text` as a visible user turn. The frame
// proposes; the operator decides — the host shows a confirm chip in console DOM and posts only
// when its Send button is clicked, unless the operator ticked "Always allow from this artifact".
// This drives the real stack end to end: nested artifact frame → embed shell
// (plugins/artifact/shell.js) → console host (ArtifactRefChip inline host → ChatSessionSlot's
// send path). The inline render itself is covered by artifact-inline.spec.ts.

const SEND_BUTTON_CODE =
  "<!doctype html><html><body>" +
  '<div id="inline-mark">INLINE OK</div>' +
  '<button id="send-btn">Send to chat</button>' +
  '<div id="send-status"></div>' +
  "<script>" +
  "document.getElementById('send-btn').addEventListener('click',function(){" +
  "window.protoArtifact.send('Add five and seven')" +
  ".then(function(){document.getElementById('send-status').textContent='sent';})" +
  ".catch(function(e){document.getElementById('send-status').textContent='rejected: '+(e&&e.message||e);});" +
  "});" +
  "</script>" +
  "</body></html>";

// The #4137 bypass: an artifact that focuses ITSELF and sends with no click at all, then keeps
// polling. Under the old gesture gate any console click + self-focus let this through.
const AUTOSEND_CODE =
  "<!doctype html><html><body>" +
  '<div id="inline-mark">INLINE OK</div>' +
  '<button id="send-btn">idle</button>' +
  '<div id="send-status"></div>' +
  "<script>" +
  "function go(){try{window.focus();document.getElementById('send-btn').focus();}catch(_){}" +
  "window.protoArtifact.send('Add five and seven')" +
  ".then(function(){document.getElementById('send-status').textContent='sent';})" +
  ".catch(function(e){document.getElementById('send-status').textContent='rejected: '+(e&&e.message||e);});}" +
  "go();setInterval(go,400);" +
  "</script>" +
  "</body></html>";

// A single-version html artifact whose markup runs a button that calls protoArtifact.send, and
// reports the Promise's outcome in #send-status so the test can read it from inside the frame.
const INLINE_STORE = {
  current: "art-inline",
  artifacts: [
    {
      id: "art-inline",
      kind: "html",
      title: "Inline calc",
      versions: [
        {
          code: SEND_BUTTON_CODE,
          ts: 1,
          by: "agent",
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function withInlineArtifact(page: Page, code: string = SEND_BUTTON_CODE) {
  const store = structuredClone(INLINE_STORE);
  store.artifacts[0].versions[0].code = code;
  // Enable the Artifact plugin for this page only (its right-dock view), same as artifact-inline.
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: store }));
  await page.route("**/api/plugins/artifact/refs?*", (route) => {
    const ids = (new URL(route.request().url()).searchParams.get("ids") || "").split(",");
    const artifacts: Record<string, unknown> = {};
    for (const a of INLINE_STORE.artifacts) {
      if (!ids.includes(a.id)) continue;
      artifacts[a.id] = { title: a.title, kind: a.kind, version_count: a.version_count, oldest: 1 };
    }
    return route.fulfill({ json: { artifacts } });
  });
}

async function openInline(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("ARTIFACTREF INLINE please");
  await composer.press("Enter");
  // The inline host + its embed frame render while the turn is still streaming. Let that first
  // turn SETTLE before driving the frame: an inline send while the agent is still streaming is
  // (correctly) refused as "the agent is busy", which is a different path than the one under test.
  await expect(page.getByTestId("artifact-ref-inline")).toBeVisible();
  await expect(page.locator(".pl-message--assistant").last()).toContainText("Here's the inline calculator.");
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
  // Wait until the nested artifact frame has mounted the model's markup (so the send button and
  // the protoArtifact shim both exist).
  await expect(page.getByTestId("artifact-ref-inline")).toBeVisible();
  await expect(sendButton(page)).toBeVisible();
}

/** The model's send button, inside the embed's nested artifact frame. */
function sendButton(page: Page) {
  return page.getByTestId("artifact-inline-frame").contentFrame().frameLocator("#frame").locator("#send-btn");
}
function sendStatus(page: Page) {
  return page.getByTestId("artifact-inline-frame").contentFrame().frameLocator("#frame").locator("#send-status");
}

const userTurns = (page: Page) => page.locator(".pl-message--user", { hasText: "Add five and seven" });

test("a send button shows the host confirm chip; clicking Send posts a labelled user turn", async ({ page }) => {
  await withInlineArtifact(page);
  await openInline(page);

  await sendButton(page).click();

  // The frame only PROPOSED: a chip in console DOM shows exactly what would be sent, and nothing
  // has been posted yet.
  const chip = page.getByTestId("artifact-send-confirm");
  await expect(chip).toBeVisible();
  await expect(page.getByTestId("artifact-send-confirm-text")).toHaveText("Add five and seven");
  await expect(userTurns(page)).toHaveCount(0);

  await page.getByTestId("artifact-send-confirm-ok").click();

  // The sent text lands as an ordinary, VISIBLE user message…
  await expect(userTurns(page)).toHaveCount(1);
  // …tagged "from ‹title›" so the operator can tell it from one they typed.
  await expect(page.getByTestId("chat-from-artifact")).toContainText("from Inline calc");
  // …and a normal turn runs (the mock's default answer), proving the send started one.
  await expect(page.locator(".pl-message--assistant").last()).toContainText("Done — found 8 results.", {
    timeout: 15_000,
  });
  // The frame's own Promise resolved through the host bridge.
  await expect(sendStatus(page)).toHaveText("sent");
  await expect(chip).toHaveCount(0);
});

test("Dismiss rejects the frame's promise and no turn starts", async ({ page }) => {
  await withInlineArtifact(page);
  await openInline(page);

  await sendButton(page).click();
  await page.getByTestId("artifact-send-confirm-dismiss").click();

  await expect(sendStatus(page)).toContainText(/rejected: .*dismissed/i);
  await expect(page.getByTestId("artifact-send-confirm")).toHaveCount(0);
  await expect(userTurns(page)).toHaveCount(0);
  // Only the original "ARTIFACTREF INLINE please" user turn exists.
  await expect(page.locator(".pl-message--user")).toHaveCount(1);
});

test("a self-focusing, polling artifact never posts on its own — even after console clicks (#4137)", async ({ page }) => {
  await withInlineArtifact(page, AUTOSEND_CODE);
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("ARTIFACTREF INLINE please");
  await composer.press("Enter");
  await expect(page.getByTestId("artifact-ref-inline")).toBeVisible();
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
  // Give the user activation every chance: click console chrome, then let the artifact poll.
  await page.getByTestId("artifact-ref-inline").locator(".artifact-ref-inline__head").click();
  await page.waitForTimeout(2_500);
  // It only ever proposes: one chip, no turn.
  await expect(page.getByTestId("artifact-send-confirm")).toHaveCount(1);
  await expect(userTurns(page)).toHaveCount(0);
  await expect(page.locator(".pl-message--user")).toHaveCount(1);
});

test("'Always allow' skips the chip for the next send from the same artifact", async ({ page }) => {
  await withInlineArtifact(page);
  await openInline(page);

  await sendButton(page).click();
  await page.getByTestId("artifact-send-confirm-trust").check();
  await page.getByTestId("artifact-send-confirm-ok").click();
  await expect(userTurns(page)).toHaveCount(1);
  await expect(page.locator(".pl-message--assistant").last()).toContainText("Done — found 8 results.", {
    timeout: 15_000,
  });
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);

  // Trusted now: the next send goes straight through (past the 2 s rate window), no chip.
  await page.waitForTimeout(2_100);
  await sendButton(page).click();
  await expect(userTurns(page)).toHaveCount(2);
  await expect(page.getByTestId("artifact-send-confirm")).toHaveCount(0);
});
