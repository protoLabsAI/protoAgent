import { expect, test } from "@playwright/test";
import { expectNoAxeViolations } from "./axe";

test("axe reports violations and supports scoped scans and explicit rule exclusions", async ({ page }) => {
  await page.setContent(`
    <!doctype html>
    <html lang="en">
      <head><title>Axe helper fixture</title></head>
      <body><main>
        <h1>Accessibility fixture</h1>
        <div id="valid"><button>Save</button></div>
        <button id="unnamed"></button>
      </main></body>
    </html>
  `);

  await expect(expectNoAxeViolations(page)).rejects.toThrow(/button-name \(critical\)[\s\S]*#unnamed/);
  await expectNoAxeViolations(page, { include: "#valid" });
  await expectNoAxeViolations(page, { disableRules: ["button-name"] });
});
