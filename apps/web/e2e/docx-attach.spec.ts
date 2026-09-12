import { expect, test } from "@playwright/test";

import { CHAT_ATTACH_ACCEPT, DOCX_MIME, KNOWLEDGE_INGEST_ACCEPT } from "../src/lib/attachTypes";

// Word .docx is a chat attachment and a Knowledge source (the jobCoach flow: drop a
// resume in chat). `setInputFiles` ignores `accept`, so the picker's attribute is
// asserted directly — it's what a real "browse" dialog filters by. Extraction itself is
// covered by tests/test_ingestion_docx.py; the mock server doesn't parse the bytes.
const DOCX = Buffer.from("PK docx bytes for the mock");

test("the chat composer's picker offers .docx and a resume attaches", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  const slot = page.locator(".chat-session-slot:not([hidden])");
  const picker = slot.locator('input[type="file"]');

  await expect(picker).toHaveAttribute("accept", CHAT_ATTACH_ACCEPT);
  expect(CHAT_ATTACH_ACCEPT.split(",")).toEqual(expect.arrayContaining([".docx", DOCX_MIME]));

  const upload = page.waitForRequest((r) => r.url().endsWith("/api/knowledge/attach") && r.method() === "POST");
  await picker.setInputFiles({ name: "resume.docx", mimeType: DOCX_MIME, buffer: DOCX });
  await upload; // went through the extraction pipeline, not the native-image path

  const chips = slot.locator(".pl-prompt__attachments");
  await expect(chips).toContainText("resume.docx");
  await expect(chips).not.toContainText("uploading");
});

test("the Knowledge page's picker offers .docx", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();
  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible();

  await surface.getByTitle(/^Add a source/).click();
  const dialog = page.getByRole("dialog", { name: "Add a source" });
  await expect(dialog.locator('input[type="file"]')).toHaveAttribute("accept", KNOWLEDGE_INGEST_ACCEPT);
  expect(KNOWLEDGE_INGEST_ACCEPT.split(",")).toEqual(expect.arrayContaining([".docx", DOCX_MIME]));
  await expect(dialog.locator(".knowledge-ingest-drop")).toContainText("docx"); // the hint names it
});
