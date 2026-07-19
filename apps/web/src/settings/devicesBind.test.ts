import { describe, expect, it } from "vitest";

// Vite `?raw` rather than node:fs — this tsconfig has no node types, and widening them for
// one guard would be a worse trade than importing the source as a string.
import src from "./DevicesPanel.tsx?raw";

// Regression guard for ADR 0087 D6 (v0.104.2).
//
// `makeReachable` must bind 0.0.0.0 — NOT the address the operator picked. uvicorn takes one
// host, and a single non-loopback bind DROPS loopback, which breaks the desktop app outright:
// its webview reaches its own sidecar over http://127.0.0.1:<port> (src-tauri/src/lib.rs).
// v0.104.1 shipped the specific-address version and hung the desktop app on launch.
//
// Asserted against the source because the failure is a one-token change that type-checks,
// passes every behavioural test, and only shows up on a machine where loopback is the
// client — i.e. not in CI.
describe("makeReachable bind target", () => {
  it("saves 0.0.0.0, so loopback survives for the desktop app", () => {
    expect(src).toContain('"network.bind": "0.0.0.0"');
  });

  it("never binds the operator's chosen address (that is the QR target, not the bind)", () => {
    expect(src).not.toMatch(/"network\.bind":\s*addr\.host/);
  });

  it("explains that it listens on ALL interfaces, not just the chosen one", () => {
    // The copy has to match reality; "listen on the address you pick" was wrong AND
    // undersold the exposure.
    expect(src).toMatch(/all<\/strong>\s*\{?"?\s*your network interfaces/);
  });
});
