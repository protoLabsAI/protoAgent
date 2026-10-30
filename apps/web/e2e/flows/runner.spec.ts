import { readFile, readdir } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import config from "../../playwright.config";
import { careerCoachFlow, careerCoachArchetype } from "./careercoach";
import { runFlow } from "./runner";

test("failure writes evidence, skips later steps, and clears retry artifacts", async ({ page, baseURL }, info) => {
  const outDir = info.outputPath("flows");
  const flow = {
    name: "failure-contract", version: 2,
    steps: [
      { name: "First", async run() { await page.setContent("<h1>Ready</h1>"); } },
      { name: "Fails", async run() {} },
      { name: "Skipped", async run() {} },
    ],
  };
  const options = { baseURL: baseURL!, mode: "mock" as const, outDir };
  expect((await runFlow(flow, page, options)).ok).toBe(true);
  flow.steps[1].run = async () => { throw new Error("deliberate failure"); };
  let laterRan = false;
  flow.steps[2].run = async () => { laterRan = true; };
  const summary = await runFlow(flow, page, options);
  expect(laterRan).toBe(false);
  expect(summary.ok).toBe(false);
  expect(summary.steps.map((step) => step.status)).toEqual(["pass", "fail", "skipped"]);
  expect(summary.steps[1].error).toContain("deliberate failure");
  expect(summary.steps[2]).toMatchObject({ ms: 0, screenshot: null });
  expect(summary.steps.every((step) => step.ms >= 0)).toBe(true);
  const dir = `${outDir}/${flow.name}`;
  expect(JSON.parse(await readFile(`${dir}/summary.json`, "utf8"))).toMatchObject({
    flow: flow.name, version: 2, base_url: baseURL, mode: "mock", ok: false,
  });
  expect((await readdir(dir)).filter((file) => file.endsWith(".png"))).toEqual(["01-first.png", "02-fails.png"]);
});

for (const deleteFails of [false, true]) {
  test(`live auth and receipt-bound cleanup (delete fails: ${deleteFails})`, async ({ page, baseURL }, info) => {
    test.setTimeout(120_000);
    // Exercise live behavior against an isolated backend without real credentials.
    const scope = `live-contract-${info.parallelIndex}`;
    await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope });
    await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope } });
    await page.route("**/api/archetypes*", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: {
        ...body, archetypes: [...body.archetypes, careerCoachArchetype],
      } });
    });
    const deleted: string[] = [];
    let created: { id: string; name: string };
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/fleet" && request.method() === "POST") {
        const name = request.postDataJSON().name;
        created = { name, id: `${name}-ab12` };
      }
    });
    await page.route("**/api/fleet/*", async (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      deleted.push(decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop()!));
      expect(route.request().headers().authorization).toBe("Bearer flow-test-token");
      if (deleteFails) return route.fulfill({ status: 500, json: { ok: false } });
      return route.continue();
    });
    const summary = await runFlow(careerCoachFlow(true), page, {
      baseURL: baseURL!, mode: "live", token: "flow-test-token", outDir: info.outputPath("flows"),
    });
    expect(created!.name).toMatch(/^flow-cc-\d+$/);
    expect(deleted).toEqual([created!.id]);
    expect(summary.mode).toBe("live");
    expect(summary.ok).toBe(!deleteFails);
    expect(summary.steps.at(-1)?.status).toBe(deleteFails ? "fail" : "pass");
    expect(summary.steps.every((step) => step.screenshot && step.ms >= 0)).toBe(true);
    const fleet = await (await page.request.get("/api/fleet", { headers: { "x-e2e-fleet": scope } })).json();
    expect(fleet.agents.some((agent: { id: string }) => agent.id === created!.id)).toBe(deleteFails);
    expect(fleet.agents.some((agent: { name: string }) => agent.name === "ava")).toBe(true);
    expect(fleet.agents.some((agent: { name: string }) => agent.name === "roxy")).toBe(true);
  });
}

test("flows project is collected independently and live config disables mock startup", () => {
  expect(config.projects?.find((project) => project.name === "flows")?.testMatch).toEqual(/flows\/.*\.flow\.ts$/);
  expect(config.testMatch).toBe("**/*.spec.ts");
  if (process.env.FLOW_BASE_URL) {
    expect(config.webServer).toBeUndefined();
    expect(config.use?.baseURL).toBe(process.env.FLOW_BASE_URL);
  } else {
    expect(config.webServer).toHaveProperty("command", expect.stringContaining("mock-server.mjs"));
  }
});

test("missing live token fails before running any flow action", async ({ page, baseURL }, info) => {
  let ran = false;
  const summary = await runFlow({
    name: "missing-token", version: 1,
    steps: [{ name: "Open", async run() { ran = true; } }],
  }, page, { baseURL: baseURL!, mode: "live", outDir: info.outputPath("flows") });
  expect(ran).toBe(false);
  expect(summary.ok).toBe(false);
  expect(summary.steps[0]).toMatchObject({ status: "fail", error: "FLOW_TOKEN is required in live mode" });
});

test("screenshot failure still writes a failed summary preserving the action error", async ({ page, baseURL }, info) => {
  const outDir = info.outputPath("flows");
  const summary = await runFlow({
    name: "closed-page", version: 1,
    steps: [{ name: "Close", async run() { await page.close(); throw new Error("original action failure"); } }],
  }, page, { baseURL: baseURL!, mode: "mock", outDir });
  expect(summary.ok).toBe(false);
  expect(summary.steps[0].screenshot).toBeNull();
  expect(summary.steps[0].error).toContain("original action failure");
  expect(JSON.parse(await readFile(`${outDir}/closed-page/summary.json`, "utf8")).ok).toBe(false);
});
