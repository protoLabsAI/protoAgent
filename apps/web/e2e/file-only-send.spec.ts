import { expect, test } from "@playwright/test";

// bd-1n7: a message can be sent with an attachment and NO typed text (e.g.
// attach a doc/image and hit send with an empty field). The DS PromptInput
// (@protolabsai/ui ≥ 0.34) enables submit when attachments are present, and the
// composer's send gate matches.
test("attach a file with no caption and send it", async ({ page }) => {
  // The A2A message the turn goes out as — asserted at the WIRE level below.
  const sent: { parts?: { text?: string }[]; metadata?: Record<string, unknown> }[] = [];
  page.on("request", (req) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      if (body?.method === "SendStreamingMessage") sent.push(body.params?.message ?? {});
    } catch {
      // non-JSON /a2a traffic — not a chat turn
    }
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");

  // Attach a small text file via the hidden picker — no text typed.
  await slot.locator('input[type="file"]').setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("hello from the attached file"),
  });

  // The attachment chip appears and settles (no longer uploading).
  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("notes.txt");
  await expect(chips).not.toContainText("uploading");

  // Send is enabled with an empty field — click it.
  const send = slot.getByRole("button", { name: "Send" });
  await expect(send).toBeEnabled();
  await send.click();

  // The turn sends: the user bubble shows the 📎 attachment line (not a raw dump),
  // and the assistant answers.
  await expect(slot.locator(".pl-message--user")).toContainText("notes.txt");
  await expect(slot.locator(".pl-message--assistant .markdown")).toContainText(
    "Done — found 8 results.",
  );

  // The model gets the attachment context; the message ALSO records the bubble it drew,
  // so a chat rebuilt from the durable turn (ADR 0104) shows the 📎 line, not the dump.
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0].parts?.[0]?.text).toContain("hello from the attached file");
  expect(sent[0].metadata?.display).toBe("Attached: notes.txt");
});
