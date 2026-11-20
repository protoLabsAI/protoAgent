import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export async function expectNoAxeViolations(
  page: Page,
  opts: { include?: string; disableRules?: string[] } = {},
) {
  // CSSOM preloading refetches external font stylesheets via fetch, which the console's
  // connect-src CSP forbids. Keep media preloading and scan the rendered styles normally.
  let builder = new AxeBuilder({ page }).options({ preload: { assets: ["media"] } });
  if (opts.include) builder = builder.include(opts.include);
  if (opts.disableRules) builder = builder.disableRules(opts.disableRules);

  const { violations } = await builder.analyze();
  const details = violations.map((violation) =>
    `${violation.id} (${violation.impact ?? "unknown impact"}): ${violation.help}\n` +
    violation.nodes.map((node) => `  - ${JSON.stringify(node.target)}: ${node.failureSummary}`).join("\n"),
  ).join("\n\n");
  expect(violations.length, `Accessibility violations:\n${details}`).toBe(0);
}
