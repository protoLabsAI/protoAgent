import { expect, test, devices, type Page } from "@playwright/test";

// Captured by executing _submit_approval_card with fill, upload and skip rows from
// protoLabsAI/careercoach-plugin@95fc87f921b890238ac444aa22a40929193815bd (__init__.py).
// Only the relative formfill constants import was stubbed; this is its complete output.
// Input rows were the three fields below plus
// { label: "Skipped salary", value: "DO NOT SHOW", action: "skip" }.
const approval = {
  "kind": "approval",
  "type": "careercoach_submit_approval",
  "title": "Approve submitting this job application?",
  "description": "Submit this job application? (Platform Engineer — Example & Co)\n\nApprove to authorize ONE submit click (valid 120s); decline to stop.",
  "detail": "  Full name -> Ada Lovelace\n  Resume -> resume.pdf  (upload)\n  Portfolio -> <a href=\"https://example.com\">openLink</a> & <img src=x onerror=alert(1)>",
  "options": [
    {
      "label": "Approve submit",
      "value": "approve",
      "primary": true
    },
    {
      "label": "Decline",
      "value": "decline"
    }
  ],
  "display": {
    "component": "table",
    "props": {
      "columns": [
        "Field",
        "Value"
      ],
      "rows": [
        [
          "Full name",
          "Ada Lovelace"
        ],
        [
          "Resume",
          "resume.pdf  (upload)"
        ],
        [
          "Portfolio",
          "<a href=\"https://example.com\">openLink</a> & <img src=x onerror=alert(1)>"
        ]
      ]
    }
  },
  "company": "Example & Co",
  "role": "Platform Engineer",
  "session_id": "careercoach-display-proof",
  "fields": [
    {
      "label": "Full name",
      "value": "Ada Lovelace",
      "action": "fill"
    },
    {
      "label": "Resume",
      "value": "resume.pdf",
      "action": "upload"
    },
    {
      "label": "Portfolio",
      "value": "<a href=\"https://example.com\">openLink</a> & <img src=x onerror=alert(1)>",
      "action": "fill"
    }
  ],
  "prompt": "Submit this job application? (Platform Engineer — Example & Co)\n\nPlanned answers:\n  Full name -> Ada Lovelace\n  Resume -> resume.pdf  (upload)\n  Portfolio -> <a href=\"https://example.com\">openLink</a> & <img src=x onerror=alert(1)>\n\nApprove to authorize ONE submit click (valid 120s); decline to stop."
};

const SLOT = ".chat-session-slot:not([hidden])";

async function openApproval(page: Page, payload: Record<string, unknown>) {
  await page.route("**/api/events**", (route) =>
    route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }));
  const sends: Array<{ parts: Array<{ text?: string }>; metadata?: { hitl_resume?: boolean }; taskId?: string }> = [];
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON();
    if (!body.params?.message) return route.continue();
    const message = body.params.message;
    sends.push(message);
    const taskId = "task-display-proof";
    const contextId = message.contextId;
    const result = sends.length === 1
      ? { kind: "status-update", taskId, contextId, status: { state: "input-required",
          message: { role: "agent", parts: [{ kind: "data", data: payload,
            metadata: { mimeType: "application/vnd.protolabs.hitl-v1+json" } }] } } }
      : { kind: "status-update", taskId, contextId, status: { state: "completed" }, final: true };
    const frames = sends.length === 1
      ? [{ kind: "task", id: taskId, contextId, status: { state: "submitted" }, artifacts: [] }, result]
      : [result];
    await route.fulfill({ contentType: "text/event-stream",
      body: frames.map((frame) => `data: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result: frame })}\n\n`).join("") });
  });
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("Review the planned application");
  await composer.press("Enter");
  const card = page.locator(`${SLOT} .hitl-card`);
  await expect(card).toBeVisible();
  await expect(card.locator(".hitl-detail")).toHaveText(approval.detail);
  await expect(card).toContainText("Platform Engineer — Example & Co");
  await expect(card.locator(".hitl-actions button")).toHaveText(["Approve submit", "Decline"]);
  return { card, sends };
}

for (const phone of [false, true]) {
  test.describe(phone ? "phone" : "desktop", () => {
    if (phone) {
      const { defaultBrowserType: _browser, ...device } = devices["iPhone 13"];
      test.use({ ...device, viewport: { width: 320, height: 740 } });
    }
    for (const mode of ["light", "dark"] as const) {
      for (const option of approval.options) {
        test(`${mode}: inert producer table then ${option.value}`, async ({ page }, testInfo) => {
          await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
          const effects: string[] = [];
          await page.exposeFunction("recordDisplayEffect", (type: string) => effects.push(type));
          await page.addInitScript(() => {
            window.addEventListener("message", (event) => {
              const type = event.data?.type;
              if (typeof type === "string" && /^(protoArtifact|protoComponent):/.test(type)) {
                (window as unknown as { recordDisplayEffect: (type: string) => void }).recordDisplayEffect(type);
              }
            });
          });
          page.on("dialog", async (dialog) => { effects.push("dialog"); await dialog.dismiss(); });
          page.on("request", (request) => {
            if (/\/api\/(plugins\/artifact|components)/.test(request.url())) effects.push(request.url());
          });
          const { card, sends } = await openApproval(page, approval);
          await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
          const table = card.getByRole("table");
          await expect(table).toBeVisible();
          await expect(table.getByRole("columnheader")).toHaveText(["Field", "Value"]);
          await expect(table.getByRole("cell")).toHaveText(approval.display.props.rows.flat());
          await expect(card).not.toContainText("Skipped salary");
          await expect(card).not.toContainText("DO NOT SHOW");
          // HTML and URLs are literal text, never a frame/bridge, openLink, or a form.
          await expect(table.locator("iframe, script, img, a, form, input, button, select, textarea, [role=button], [contenteditable=true]")).toHaveCount(0);
          effects.length = 0;
          const url = page.url();
          for (const cell of await table.getByRole("cell").all()) await cell.click();
          // Allow debounced/async click handlers time to send; capture ALL chat turns.
          await page.waitForTimeout(300);
          expect(sends).toHaveLength(1);
          expect(effects).toEqual([]);
          expect(page.url()).toBe(url);
          expect(page.context().pages()).toHaveLength(1);
          await expect(card).toBeVisible();
          if (phone) {
            const display = card.locator(".hitl-display");
            expect(await display.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
            // The card scrolls vertically; every cell and both host actions remain reachable.
            await card.getByRole("button", { name: option.label, exact: true }).scrollIntoViewIfNeeded();
            await expect(card.getByRole("button", { name: option.label, exact: true })).toBeInViewport();
          }
          await card.locator(".hitl-display").evaluate((el) => { el.scrollTop = 0; });
          const evidence = `hitl-${phone ? "phone" : "desktop"}-${mode}-${option.value}`;
          const screenshot = testInfo.outputPath(`${evidence}.png`);
          await page.screenshot({ path: screenshot });
          await testInfo.attach(evidence, { path: screenshot, contentType: "image/png" });
          await card.getByRole("button", { name: option.label, exact: true }).click();
          await expect.poll(() => sends.length).toBe(2);
          expect(sends[1].metadata?.hitl_resume).toBe(true);
          expect(sends[1].taskId).toBe("task-display-proof");
          expect(sends[1].parts).toEqual([{ text: option.value }]);
          await expect(card).toHaveCount(0);
        });
      }
    }
    if (phone) {
      test("wide bounded table scrolls inside the phone card", async ({ page }) => {
        // Deliberate contract stress variant, separate from the captured producer fixture.
        const columns = Array.from({ length: 16 }, (_, i) => `Column ${i + 1}`);
        const { card, sends } = await openApproval(page, { ...approval,
          display: { component: "table", props: { columns, rows: [columns] } },
        });
        const display = card.locator(".hitl-display");
        const scroll = await display.evaluate((el) => {
          el.scrollLeft = el.scrollWidth;
          return { left: el.scrollLeft, width: el.clientWidth, total: el.scrollWidth };
        });
        expect(scroll.total).toBeGreaterThan(scroll.width);
        expect(scroll.left).toBeGreaterThan(0);
        await expect(card.getByRole("cell", { name: "Column 16", exact: true })).toBeInViewport();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        expect(sends).toHaveLength(1);
        await card.getByRole("button", { name: "Decline", exact: true }).click();
        await expect.poll(() => sends.length).toBe(2);
        expect(sends[1].parts).toEqual([{ text: "decline" }]);
        expect(sends[1].metadata?.hitl_resume).toBe(true);
      });
    }
    for (const [name, display] of [
      ["absent", undefined],
      ["unsupported", { component: "plugin:approval", props: approval.display.props }],
      ["invalid", { component: "table", props: { columns: ["Field", "Value"], rows: [["ragged"]] } }],
    ] as const) {
      for (const option of approval.options) {
        test(`${name} display retains fallback and ${option.value}`, async ({ page }) => {
          const { card, sends } = await openApproval(page, { ...approval, display });
          await expect(card.getByRole("table")).toHaveCount(0);
          await expect(card).toContainText("Approve to authorize ONE submit click (valid 120s); decline to stop.");
          await card.getByRole("button", { name: option.label, exact: true }).click();
          await expect.poll(() => sends.length).toBe(2);
          expect(sends[1].metadata?.hitl_resume).toBe(true);
          expect(sends[1].parts).toEqual([{ text: option.value }]);
          await expect(card).toHaveCount(0);
        });
      }
    }
  });
}
