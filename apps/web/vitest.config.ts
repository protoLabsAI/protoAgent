import { defineConfig } from "vitest/config";

// Unit tests for the console's pure logic (chat-store reducers, the A2A SSE
// frame parser, the uiStore persist migration). jsdom because the modules under
// test touch `window`/`localStorage` at import time, even though the functions
// themselves are pure. E2E (Playwright) lives separately under e2e/.
export default defineConfig({
  test: {
    environment: "jsdom",
    // Repairs the Web Storage globals on Nodes that pre-define them (25+), where jsdom's
    // would otherwise never be installed. See the file for the full failure mode (#3213).
    setupFiles: ["./vitest.setup.ts"],
    // `.tsx` is included alongside `.ts` so component tests that mount React through
    // react-dom/client (e.g. app/FleetRoom.test.tsx, the #3169 diagnostics drawer) run in the
    // same suite. esbuild transforms the TSX per this workspace's tsconfig `jsx` setting.
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
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
