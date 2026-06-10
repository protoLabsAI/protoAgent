import { defineConfig } from "vitest/config";

// Unit tests for the console's pure logic (chat-store reducers, the A2A SSE
// frame parser, the uiStore persist migration). jsdom because the modules under
// test touch `window`/`localStorage` at import time, even though the functions
// themselves are pure. E2E (Playwright) lives separately under e2e/.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
    globals: false,
  },
});
