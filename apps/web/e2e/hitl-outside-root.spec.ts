import { expect, test } from "@playwright/test";

// An approval that carries its OWN choices (register_local_project outside the
// onboarding root): the card renders exactly the server's options, each resumes with
// its value verbatim (the server binds it to the folder on the card), and — because
// session_allow is false — there is no "Approve & don't ask again".

const SLOT = ".chat-session-slot:not([hidden])";

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("an outside-root approval renders its own choices and resumes with the chosen value", async ({ page }) => {
  await send(page, "HITL_OUTSIDE_ROOT: explore mundamanager");
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();
  await expect(card).toContainText("Allow access to a folder outside the onboarding root?");
  await expect(card.locator(".hitl-detail")).toContainText("Folder:     /Users/op/dev/mundamanager");

  const buttons = card.locator(".hitl-actions button");
  await expect(buttons).toHaveText(["Allow read-only", "Allow read-write", "Deny"]);
  await expect(card.getByRole("button", { name: /don.t ask again/i })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);

  const resume = page.waitForRequest((req) => req.method() === "POST" && (req.postData() || "").includes("hitl_resume"));
  await card.getByRole("button", { name: "Allow read-only" }).click();
  const body = (await resume).postData() || "";
  expect(body).toContain("allow-read-only@0123456789ab");
  await expect(card).toHaveCount(0);
});
