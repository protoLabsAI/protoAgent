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
    // ""). The source-guard tests read stylesheets as raw text — mobileBottomInset.test.ts
    // (mobile safe-area insets, #2086), the two hitl-accent tests (accent chains, #2153),
    // and app/statusTokenGuard.test.ts, which sweeps EVERY console stylesheet for phantom
    // status tokens (#2224) — so all of src's CSS is opted into processing (the path anchor
    // keeps node_modules CSS stubbed, so DS component imports are unaffected). The sweep
    // asserts each file's raw text is non-empty, so narrowing this back to a per-file list
    // fails loudly instead of silently blinding the guard.
    css: { include: [/apps\/web\/src\/.*\.css/] },
  },
});
