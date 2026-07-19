import { beforeEach, describe, expect, it, vi } from "vitest";

// Desktop self-auth (#2055). The desktop app spawns its own server and knows that server's
// token, so a 401 in the app should resolve itself instead of asking the operator to dig a
// secret out of secrets.yaml to unlock software on their own machine.

const KEY = "protoagent.authToken";

function withTauri(invoke: (cmd: string) => Promise<unknown>) {
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke } };
}

describe("notifyAuthRequired → desktop self-auth", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
  });

  it("adopts the shell's token on a 401 and clears the gate", async () => {
    withTauri(async () => "shell-token");
    const auth = await import("./auth");
    auth.notifyAuthRequired();
    await vi.waitFor(() => expect(window.localStorage.getItem(KEY)).toBe("shell-token"));
    expect(auth.authRequired()).toBe(false); // saveAuthToken clears it — no prompt shown
  });

  it("leaves the gate up in a browser (no Tauri shell)", async () => {
    const auth = await import("./auth");
    auth.notifyAuthRequired();
    expect(auth.authRequired()).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("does NOT re-save a token the server just rejected", async () => {
    // The 401 came from THIS token, so re-saving it would clear the gate, retry, 401
    // again — an invisible loop instead of a prompt.
    window.localStorage.setItem(KEY, "already-rejected");
    withTauri(async () => "already-rejected");
    const auth = await import("./auth");
    auth.notifyAuthRequired();
    await new Promise((r) => setTimeout(r, 20));
    expect(auth.authRequired()).toBe(true); // prompt still required
  });

  it("falls through to the prompt when the shell has no token", async () => {
    withTauri(async () => null);
    const auth = await import("./auth");
    auth.notifyAuthRequired();
    await new Promise((r) => setTimeout(r, 20));
    expect(auth.authRequired()).toBe(true);
  });

  it("falls through when the command is missing (older shell)", async () => {
    withTauri(async () => {
      throw new Error("command not found");
    });
    const auth = await import("./auth");
    auth.notifyAuthRequired();
    await new Promise((r) => setTimeout(r, 20));
    expect(auth.authRequired()).toBe(true); // degrades, never blocks boot
  });
});
