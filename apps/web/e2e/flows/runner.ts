import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

export interface Flow {
  name: string;
  version: number;
  steps: { name: string; run(page: Page): Promise<void> }[];
}

export interface FlowOptions {
  baseURL: string;
  mode: "mock" | "live";
  outDir?: string;
  token?: string;
}

export async function runFlow(flow: Flow, page: Page, options: FlowOptions) {
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(flow.name)) throw new Error("Flow name must be a slug");
  const out = path.resolve(options.outDir ?? process.env.FLOW_OUT_DIR ?? "test-results/flows", flow.name);
  const summary = {
    flow: flow.name, version: flow.version, base_url: options.baseURL,
    mode: options.mode, started_at: new Date().toISOString(), ok: false,
    steps: flow.steps.map(({ name }) => ({
      name, status: "skipped" as "pass" | "fail" | "skipped", ms: 0,
      screenshot: null as string | null, error: undefined as string | undefined,
    })),
  };
  await mkdir(out, { recursive: true });
  // A retry must not leave a successful summary or screenshots from an older attempt.
  await rm(path.join(out, "summary.json"), { force: true });
  const screenshots = flow.steps.map(({ name }, i) =>
    `${String(i + 1).padStart(2, "0")}-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "step"}.png`);
  await Promise.all(screenshots.map((file) => rm(path.join(out, file), { force: true })));
  try {
    for (const [i, step] of flow.steps.entries()) {
      const result = summary.steps[i];
      const started = performance.now();
      try {
        await test.step(step.name, async () => {
          let failure: unknown;
          try {
            if (i === 0 && options.mode === "live") {
              if (!options.token) throw new Error("FLOW_TOKEN is required in live mode");
              await page.addInitScript((token) => localStorage.setItem("protoagent.authToken", token), options.token);
            }
            await step.run(page);
          } catch (error) {
            failure = error instanceof Error ? error : new Error(String(error));
          }
          try {
            await page.screenshot({ path: path.join(out, screenshots[i]), fullPage: true, timeout: 5_000 });
            result.screenshot = screenshots[i];
          } catch (error) {
            failure = failure ? new Error(`${String(failure)}; screenshot: ${String(error)}`) : error;
          }
          if (failure) throw failure;
        }, { timeout: 60_000 });
        result.status = "pass";
      } catch (error) {
        result.status = "fail";
        result.error = error instanceof Error ? error.message : String(error);
        // Auth values must not escape through assertion / request error messages.
        if (options.token) result.error = result.error.split(options.token).join("[redacted]");
      } finally {
        result.ms = Math.round(performance.now() - started);
      }
      if (result.status === "fail") break;
    }
    summary.ok = summary.steps.every((step) => step.status === "pass");
    return summary;
  } finally {
    await writeFile(path.join(out, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  }
}

export function defineFlow(flow: Flow) {
  test(`${flow.name} v${flow.version}`, async ({ page, baseURL }) => {
    test.setTimeout((flow.steps.length + 1) * 65_000);
    const summary = await runFlow(flow, page, {
      baseURL: baseURL!, mode: process.env.FLOW_BASE_URL ? "live" : "mock", token: process.env.FLOW_TOKEN,
    });
    expect(summary.ok, summary.steps.find((step) => step.error)?.error).toBe(true);
  });
}
