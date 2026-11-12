import { expect, test } from "@playwright/test";

const SESSION = "chat-directive-e2e";
const QUOTE = "Cut the release once both PRs merge";
const APPROVAL_QUOTE = "Approve the dependency bumps";
const NOTE = 'You withdrew: "Pause deploys" — freeze lifted';

test("a fresh chat loads an empty directive ledger: no Told it chip, Work rail clean", async ({ page }) => {
  const directives = page.waitForResponse((response) =>
    /\/api\/chat\/sessions\/[^/]+\/directives$/.test(new URL(response.url()).pathname));
  await page.goto("/app/");
  const response = await directives;
  expect(response.ok()).toBe(true);
  expect(await response.json()).toEqual({ directives: [], in_force: 0 });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/directives ");
  await composer.press("Enter");
  const rail = page.locator('.work-view[data-frame="rail"]');
  await expect(rail.locator(".work-section--directives")).toBeVisible();
  await expect(rail.getByText("No standing instructions", { exact: true })).toBeVisible();
  // Zero instructions → no chip; the status rail reserves no space for an empty ledger.
  await expect(page.getByTestId("chat-status-chip-instructions")).toHaveCount(0);
  await expect(rail.getByRole("alert")).toHaveCount(0);
});

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
              protoagent_directive_withdrawn: { id: "dir-old" } }] },
            { id: "other-chat", title: "Other chat", createdAt: 3, updatedAt: 3, messages: [] }],
        }));
      }, { session: SESSION, note: NOTE });
      await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
      let instruction = {
        id: "dir-one", session_id: SESSION, quote: QUOTE, conditions: "smoke tests pass",
        scope: "release", status: "in_force", created_at: "2026-11-07T17:14:02+00:00",
        closed_by: null as string | null, closed_at: null as string | null, close_reason: null as string | null,
      };
      // An approval (§3.1): a decision in the same chat answered 60s before this directive was
      // recorded, so the interim linker tags it Approval. No scope — the headline is the quote.
      const approval = {
        id: "dir-two", session_id: SESSION, quote: APPROVAL_QUOTE, conditions: "",
        scope: "", status: "in_force", created_at: "2026-11-08T19:01:00+00:00",
        closed_by: null, closed_at: null, close_reason: null,
      };
      const decided = {
        id: "dec-e2e", session_id: SESSION, agent: "protoAgent", question: "Can I bump the dependencies?",
        options: [{ id: "go", label: "Go ahead" }, { id: "stop", label: "Hold off" }],
        created_at: "2026-11-08T18:58:00+00:00", state: "answered", answer: "go",
        answered_at: "2026-11-08T19:00:00+00:00", expires_at: null, default_option: null,
        thread_id: "t-dec", interrupt_id: "i-dec", context: "", default_action: null,
        withdraw_reason: null, answered_by: "operator", answered_surface: "console",
        default_action_text: "carry on without this step", answer_label: "Go ahead",
      };
      let reason: string | null = null;
      await page.route(`**/api/chat/sessions/${SESSION}/directives**`, async (route) => {
        if (route.request().method() === "DELETE") {
          reason = new URL(route.request().url()).searchParams.get("reason");
          instruction = { ...instruction, status: "revoked", closed_by: "operator",
            closed_at: "2026-11-08T19:20:00+00:00", close_reason: reason };
          await route.fulfill({ json: { directive: instruction } });
        } else {
          const directives = [instruction, approval];
          await route.fulfill({ json: { directives, in_force: directives.filter((d) => d.status === "in_force").length } });
        }
      });
      // The §3.1 linker reads the ANSWERED ledger (the Work section tops the shared store up on
      // mount); the pending list stays empty. Single-record GETs serve the same settled row.
      await page.route("**/api/decisions**", (route) => {
        const url = new URL(route.request().url());
        if (/\/api\/decisions\/[^/]+$/.test(url.pathname)) return route.fulfill({ json: decided });
        const state = url.searchParams.get("state") ?? "pending";
        return route.fulfill({ json: { decisions: state === "answered" ? [decided] : [], pending: 0 } });
      });
      await page.route("**/api/events**", (route) => route.fulfill({
        contentType: "text/event-stream", body: ": idle\n\n",
      }));
      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
      // The Told it chip (S6a): the full name on desktop, the number alone on the phone —
      // the complete label is spoken either way. Two in force: the instruction AND the §3.1
      // approval ride the same shared-selector count the Work rows show.
      const chip = page.getByTestId("chat-status-chip-instructions");
      // useInnerText: the phone hides the wordy span with CSS; textContent would still see it.
      await expect(chip).toHaveText(width === 390 ? "2" : "Told it 2", { useInnerText: true });
      await expect(chip).toHaveAttribute(
        "aria-label",
        "Told it: 2 instructions in force in this chat. Show details",
      );
      await expect(page.locator(".pl-message--system").filter({ hasText: NOTE })).toBeVisible();
      await expect(page.locator(".pl-message--user").filter({ hasText: NOTE })).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("strip.png"), fullPage: true, animations: "disabled" });
      // Grouped details — a popover at 1440, the bottom sheet at 390 — then Open in Work.
      await chip.click();
      const details = page.locator(".chat-status-details");
      await expect(details).toContainText("Your instructions · 2");
      await expect(details).toContainText(QUOTE);
      if (width === 390) await expect(page.locator(".pl-drawer--bottom")).toBeVisible();
      await page.screenshot({ path: info.outputPath("details.png"), fullPage: true, animations: "disabled" });
      await details.getByRole("button", { name: "Open in Work" }).click();
      const rail = page.locator('.work-view[data-frame="rail"]');
      await expect(page.getByRole("dialog", { name: "Standing instructions" })).toHaveCount(0);
      // Work has no This chat / All toggle (Josh 2026-11-11): the toggle is gone from the rail.
      await expect(rail.getByRole("button", { name: "This chat", exact: true })).toHaveCount(0);
      // §3: an Instruction row headlines the scope, with muted "You said …" provenance below.
      const instructionRow = rail.locator(".work-row").filter({ hasText: 'You said "Cut the release' });
      await expect(instructionRow).toContainText("release");
      await expect(instructionRow).toContainText("Instruction");
      // §3.1: the linked row is an Approval, headlining the quote (no scope) and the chosen option.
      const approvalRow = rail.locator(".work-row").filter({ hasText: APPROVAL_QUOTE });
      await expect(approvalRow).toContainText("Approval");
      await expect(approvalRow).toContainText('You chose "Go ahead"');
      await page.screenshot({ path: info.outputPath("rail.png"), fullPage: true, animations: "disabled" });

      // The approval's detail: You were asked / You chose / Your words / Open decision — which
      // opens the decision IN PLACE through the Work detail, never the source chat.
      await approvalRow.click();
      const detail = rail.locator(".work-view__detail");
      await expect(detail).toContainText("You were asked");
      await expect(detail).toContainText("Can I bump the dependencies?");
      await expect(detail).toContainText('You chose');
      await expect(detail).toContainText(`You said "${APPROVAL_QUOTE}" in this chat`);
      await page.screenshot({ path: info.outputPath("approval.png"), fullPage: true, animations: "disabled" });
      await detail.getByRole("button", { name: "Open decision" }).click();
      await expect(rail.locator(".work-decision-detail")).toContainText("Can I bump the dependencies?");
      await rail.getByRole("button", { name: "Back to Your instructions" }).click();

      // Withdrawal through the existing confirm path, from the instruction's detail.
      await instructionRow.click();
      await detail.getByRole("button", { name: "Withdraw…" }).click();
      await detail.getByLabel("Reason (optional)").fill("Hold until Monday");
      await page.screenshot({ path: info.outputPath("confirm.png"), fullPage: true, animations: "disabled" });
      await detail.getByRole("button", { name: "Withdraw", exact: true }).click();
      await expect(page.locator(".pl-toast__msg").filter({ hasText: "Withdrawn." })).toBeVisible();
      expect(reason).toBe("Hold until Monday");
      // The row leaves the in-force list, but the open detail keeps the closed record, read-only.
      await expect(rail.locator(".work-row").filter({ hasText: 'You said "Cut the release' })).toHaveCount(0);
      await expect(detail).toContainText("Withdrawn by you");
      await expect(detail.locator(".work-detail__field").filter({ hasText: "Note" })).toContainText("Hold until Monday");
      await expect(detail.getByRole("button", { name: "Withdraw…" })).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("withdrawn.png"), fullPage: true, animations: "disabled" });

      if (width === 390) await page.getByRole("button", { name: "Back", exact: true }).click();
      // The withdrawn instruction leaves the count; the still-in-force approval keeps the chip up.
      await expect(chip).toHaveText(width === 390 ? "1" : "Told it 1", { useInnerText: true });
      await expect(page.locator(".pl-message--system").filter({ hasText: 'You withdrew: "' + QUOTE + '" — Hold until Monday' })).toBeVisible();
      const composer = page.getByPlaceholder(/Message protoAgent/i);
      await composer.fill("/directives ");
      await composer.press("Enter");
      await expect(rail.locator(".work-section--directives")).toBeVisible();
      if (width === 390) await page.getByRole("button", { name: "Back", exact: true }).click();
      await expect(composer).toHaveValue("");
      await page.keyboard.press("Escape");
      await page.keyboard.press("ControlOrMeta+Shift+k");
      await page.locator(".pl-cmdk-commands__input").fill("/directives");
      await page.getByRole("option", { name: "/directives · Standing instructions" }).click();
      await expect(rail.locator(".work-section--directives")).toBeVisible();
      await page.keyboard.press("Escape");
      if (width === 1440) {
        await page.locator(".pl-tabbar__tab").filter({ hasText: "Other chat" }).click();
        await page.locator(".pl-tabbar__tab").filter({ hasText: "Release instructions" }).click({ button: "right" });
        await page.getByRole("menuitem", { name: "Standing instructions", exact: true }).click();
        await expect(rail.locator(".work-section--directives")).toBeVisible();
        await expect(rail.locator(".work-row").filter({ hasText: APPROVAL_QUOTE })).toBeVisible();
      }
    });
  }
}
