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
    // ""). mobileBottomInset.test.ts reads mobile-shell.css/theme.css as raw text to guard
    // the mobile safe-area insets (#2086), hitl-accent.test.ts reads hitl.css to guard
    // the HITL accent chain (#2153), and chat/__tests__/hitl-accent.test.ts reads chat.css
    // to guard the success-note accent chain; processing ONLY them keeps every other CSS
    // import stubbed, so the rest of the suite is unaffected.
    css: { include: [/mobile-shell\.css/, /theme\.css/, /hitl\.css/, /chat\.css/] },
  },
});
