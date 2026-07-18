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
  projects: [
    // Desktop drives everything EXCEPT the mobile specs — they'd fail at 1200px by design
    // (the chat-first shell only exists below 768px, ADR 0086).
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: /mobile\.spec\.ts/,
    },
    // A real device profile, not just a narrow viewport: `isMobile` + `hasTouch` + DPR + UA
    // all differ, and touch is what the shell is built around. Mobile regressions went
    // unnoticed for months because the only coverage was a viewport override on a desktop
    // profile — this project is what keeps the touch floor (ADR 0086 D6) from eroding.
    //
    // Engine forced to chromium: the iPhone descriptor defaults to WebKit, and CI installs
    // chromium only (checks.yml) — pulling WebKit in would cost a browser download on every
    // run. The trade is explicit and acceptable because these specs assert COMPUTED VALUES
    // (font-size < 16px, hit-box < 44px), which every engine computes identically. What this
    // project cannot catch is iOS-specific *behaviour* — the focus zoom itself, real keyboard
    // geometry, safe-area insets. Those need a device pass; no headless engine simulates them.
    {
      name: "mobile",
      use: { ...devices["iPhone 13"], browserName: "chromium" },
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: {
    command: `node e2e/mock-server.mjs ${PORT}`,
    url: `${BASE}/app/`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
