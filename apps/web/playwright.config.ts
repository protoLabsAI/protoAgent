import { defineConfig, devices } from "@playwright/test";

// E2E smoke harness for the operator console. Drives the *built* SPA against
// the deterministic mock backend (e2e/mock-server.mjs) — no Python/model.
// `npm run test:e2e` builds the app first (see package.json), then this config
// boots the mock server and runs the specs against it.

const PORT = Number(process.env.E2E_PORT || 4319);
const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE,
    trace: "on-first-retry",
    viewport: { width: 1200, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `node e2e/mock-server.mjs ${PORT}`,
    url: `${BASE}/app/`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
