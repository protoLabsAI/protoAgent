import { describe, expect, it } from "vitest";

// Vite `?raw` rather than node:fs — this tsconfig has no node types, and widening them for
// one guard would be a worse trade than importing the source as a string.
import src from "./DevicesPanel.tsx?raw";

// Regression guard for ADR 0087 D6 (v0.104.2), revised for #4336 S4.
//
// A single non-loopback bind used to DROP loopback (uvicorn takes one host), which breaks the
// desktop app outright: its webview reaches its own sidecar over http://127.0.0.1:<port>.
// v0.104.1 shipped the specific-address version and hung the desktop app on launch.
//
// Since S4 the server opens the picked interface AND 127.0.0.1 itself (`server/bind.py`) and
// says so with `bind_interface: true`. So the panel binds the picked address ONLY when the
// server reports that, and keeps writing 0.0.0.0 for an older server that would drop
// loopback. Asserted against the source: the failure is a one-token change that
// type-checks, passes every behavioural test, and only shows up on a machine where loopback
// is the client — i.e. not in CI.
describe("makeReachable bind target", () => {
  it("binds the picked address only when the server co-binds loopback", () => {
    expect(src).toContain('const target = reach?.bind_interface ? addr.host : "0.0.0.0";');
    expect(src).toContain('{ "network.bind": target }');
  });

  it("never writes the picked address unconditionally", () => {
    expect(src).not.toMatch(/"network\.bind":\s*addr\.host/);
  });

  it("says what it listens on, for both server generations", () => {
    expect(src).toContain("listen on the address you pick, and on localhost so this app keeps");
    // The older-server copy still says ALL interfaces — it must match reality there too.
    expect(src).toMatch(/all<\/strong>\s*your network interfaces/);
  });
});
