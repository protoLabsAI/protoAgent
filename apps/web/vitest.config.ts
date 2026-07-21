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
    // By default Vitest stubs every CSS import to an empty module (so a `?raw` import yields
    // ""). mobileBottomInset.test.ts reads these two stylesheets as raw text to guard the
    // mobile safe-area insets (#2086); processing ONLY them keeps every other CSS import
    // stubbed, so the rest of the suite is unaffected.
    css: { include: [/mobile-shell\.css/, /theme\.css/] },
  },
});
