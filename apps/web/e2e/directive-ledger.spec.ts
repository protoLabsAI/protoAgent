import { expect, test } from "@playwright/test";

const SESSION = "chat-directive-e2e";
const QUOTE = "Cut the release once both PRs merge";
const NOTE = 'You withdrew: "Pause deploys" — freeze lifted';

for (const width of [1440, 390]) {
  for (const mode of ["light", "dark"]) {
    test(`ledger withdrawal and entry points at ${width} ${mode}`, async ({ page }, info) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(({ session, note }) => {
        localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
          version: 1, currentSessionId: session,
          sessions: [{ id: session, title: "Release instructions", createdAt: 1, updatedAt: 2,
            messages: [{ id: "withdrawal", role: "user", content: note, status: "done",
              protoagent_directive_withdrawn: { id: "dir-old" } }] }],
        }));
      }, { session: SESSION, note: NOTE });
      await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
      let directive = {
        id: "dir-one", session_id: SESSION, quote: QUOTE, conditions: "smoke tests pass",
        scope: "release", status: "in_force", created_at: "2026-11-07T17:14:02+00:00",
        closed_by: null as string | null, closed_at: null as string | null, close_reason: null as string | null,
      };
      let reason: string | null = null;
      await page.route(`**/api/chat/sessions/${SESSION}/directives**`, async (route) => {
        if (route.request().method() === "DELETE") {
          reason = new URL(route.request().url()).searchParams.get("reason");
          directive = { ...directive, status: "revoked", closed_by: "operator",
            closed_at: "2026-11-08T19:20:00+00:00", close_reason: reason };
          await route.fulfill({ json: { directive } });
        } else {
          await route.fulfill({ json: { directives: [directive], in_force: directive.status === "in_force" ? 1 : 0 } });
        }
      });
      await page.route("**/api/events**", (route) => route.fulfill({
        contentType: "text/event-stream", body: ": idle\n\n",
      }));
      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
      const strip = page.locator(".chat-ledger");
      await expect(strip).toContainText("1 active");
      await expect(page.locator(".pl-message--system").filter({ hasText: NOTE })).toBeVisible();
      await expect(page.locator(".pl-message--user").filter({ hasText: NOTE })).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("strip.png"), fullPage: true, animations: "disabled" });
      await strip.getByRole("button", { name: "View" }).click();
      const drawer = page.getByRole("dialog", { name: "Standing instructions" });
      await expect(drawer.getByText(QUOTE, { exact: true })).toBeVisible();
      await page.screenshot({ path: info.outputPath("drawer.png"), fullPage: true, animations: "disabled" });
      await drawer.getByRole("button", { name: "Withdraw…" }).click();
      await drawer.getByLabel("Reason (optional)").fill("Hold until Monday");
      await page.screenshot({ path: info.outputPath("confirm.png"), fullPage: true, animations: "disabled" });
      await drawer.getByRole("button", { name: "Withdraw", exact: true }).click();
      await expect(drawer.getByText("Nothing is active right now.")).toBeVisible();
      expect(reason).toBe("Hold until Monday");
      await drawer.getByRole("button", { name: "History (1)" }).click();
      await expect(drawer.getByText("Note: Hold until Monday")).toBeVisible();
      await page.screenshot({ path: info.outputPath("history.png"), fullPage: true, animations: "disabled" });
      await page.keyboard.press("Escape");
      await expect(drawer).toBeHidden();
      await expect(strip).toHaveClass(/chat-ledger--idle/);
      await expect(page.locator(".pl-message--system").filter({ hasText: 'You withdrew: "' + QUOTE + '" — Hold until Monday' })).toBeVisible();
      const composer = page.getByPlaceholder(/Message protoAgent/i);
      await composer.fill("/directives ");
      await composer.press("Enter");
      await expect(drawer).toBeVisible();
      await expect(composer).toHaveValue("");
      await page.keyboard.press("Escape");
      await page.keyboard.press("ControlOrMeta+Shift+k");
      await page.locator(".pl-cmdk-commands__input").fill("/directives");
      await page.getByRole("option", { name: "/directives · Standing instructions" }).click();
      await expect(drawer).toBeVisible();
      await page.keyboard.press("Escape");
      if (width === 1440) {
        await page.locator(".pl-tabbar__tab").filter({ hasText: "Release instructions" }).click({ button: "right" });
        await page.getByRole("menuitem", { name: "Standing instructions", exact: true }).click();
        await expect(drawer).toBeVisible();
      }
    });
  }
}
