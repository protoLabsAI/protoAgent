import { expect, test, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Send-to-chat bridge (ADR 0118 D4 / S10b): an inline artifact frame may call
// `window.protoArtifact.send(text)` to put `text` into the chat AS A VISIBLE USER TURN and start
// a normal turn — but only from a real user gesture, and the host (not the model-authored frame)
// owns that gate. This drives the real stack end to end: nested artifact frame → embed shell
// (plugins/artifact/shell.js) → console host (ArtifactRefChip inline host → ChatSessionSlot's
// send path). The inline render itself is covered by artifact-inline.spec.ts; here we exercise
// the bridge: a gesture-backed send posts a labelled turn, a gestureless send is refused.

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
          code:
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
            "</body></html>",
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

async function withInlineArtifact(page: Page) {
  // Enable the Artifact plugin for this page only (its right-dock view), same as artifact-inline.
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: INLINE_STORE }));
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

test.beforeEach(async ({ page }) => {
  await withInlineArtifact(page);
});

test("clicking a send button posts a labelled user turn and starts a turn", async ({ page }) => {
  await openInline(page);

  // A real click carries a user gesture, which User Activation v2 propagates up to the host.
  await sendButton(page).click();

  // The sent text lands as an ordinary, VISIBLE user message…
  const userTurn = page.locator(".pl-message--user", { hasText: "Add five and seven" });
  await expect(userTurn).toBeVisible();
  // …tagged "from ‹title›" so the operator can tell it from one they typed.
  await expect(page.getByTestId("chat-from-artifact")).toContainText("from Inline calc");
  // …and a normal turn runs (the mock's default answer), proving the send started one.
  await expect(page.locator(".pl-message--assistant").last()).toContainText("Done — found 8 results.", {
    timeout: 15_000,
  });
  // The frame's own Promise resolved through the host bridge.
  await expect(sendStatus(page)).toHaveText("sent");
});

test("a send with no user activation is refused and no turn starts", async ({ page }) => {
  // Force the host's OWN activation to report inactive, so even a click reads as gestureless —
  // the deterministic stand-in for a send fired on load / a timer (which the gate also refuses).
  await page.addInitScript(() => {
    try {
      Object.defineProperty(Navigator.prototype, "userActivation", {
        configurable: true,
        get: () => ({ isActive: false, hasBeenActive: false }),
      });
    } catch {
      /* leave the native value if it can't be overridden */
    }
  });
  await openInline(page);

  await sendButton(page).click();

  // The host refuses in place and tells the frame why; nothing is sent and no turn starts.
  await expect(page.getByTestId("artifact-send-rejected")).toContainText(/click or key press/i);
  await expect(sendStatus(page)).toContainText("rejected");
  await expect(page.locator(".pl-message--user", { hasText: "Add five and seven" })).toHaveCount(0);
  // Only the original "ARTIFACTREF INLINE please" user turn exists.
  await expect(page.locator(".pl-message--user")).toHaveCount(1);
});
