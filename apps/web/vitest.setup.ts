// Make the Web Storage globals the SAME on every Node the suite might run on (#3213).
//
// Node 20-25 ship no Web Storage globals, so vitest's jsdom environment installs jsdom's
// `localStorage` / `sessionStorage` and everything works. Node 26 defines its own accessors for
// both on `globalThis` (the `--experimental-webstorage` surface), and vitest's global population
// leaves a pre-existing accessor in place — so on Node 26:
//
//   * `localStorage` reads `undefined` (Node only backs it when the process was started with
//     `--localstorage-file`), which killed 127 tests across 18 files at `localStorage.clear()`
//     — on a clean tree, looking for all the world like the repo was broken;
//   * `sessionStorage` silently resolves to NODE's Storage instead of jsdom's, so those tests
//     pass against a different object than the console talks to in a browser.
//
// CI pins Node 20 and never saw either. `.nvmrc` points humans at the same version, but that is
// convenience: this file is the correctness half, so a future Node can't quietly re-break the
// suite. jsdom's own Storage is what gets installed, deliberately — a Map-backed stand-in would
// drift on the semantics the console relies on (string coercion, `null` for a missing key,
// named-property access). It has to come from a fresh JSDOM: once the environment is populated,
// `document.defaultView` IS `globalThis`, so there is no intact jsdom window left to borrow from.

import { JSDOM } from "jsdom";

/** Can this global actually store and return a value? Node's Storage passes; a missing or
 *  throwing one doesn't. Deliberately behavioral — realm identity says nothing about usability. */
function isUsableStorage(name: "localStorage" | "sessionStorage"): boolean {
  const probeKey = "__protoagent_storage_probe__";
  try {
    const storage = (globalThis as Record<string, unknown>)[name] as Storage | undefined;
    if (!storage || typeof storage.setItem !== "function") return false;
    storage.setItem(probeKey, "1");
    const ok = storage.getItem(probeKey) === "1";
    storage.removeItem(probeKey);
    return ok;
  } catch {
    return false; // accessing or using it threw — not usable
  }
}

// Repair BOTH when either is broken: a half-repaired pair (jsdom localStorage beside Node's
// sessionStorage) is its own confusing state, and one fresh window gives both the same realm
// and the same per-file lifetime.
if (!isUsableStorage("localStorage") || !isUsableStorage("sessionStorage")) {
  // A real origin — jsdom refuses Storage on an opaque one (about:blank throws SecurityError).
  const { window } = new JSDOM("", { url: "http://localhost" });
  // The `Storage` CONSTRUCTOR has to come across with the instances, not just the instances.
  // `vi.spyOn(Storage.prototype, "setItem")` — how the chat-store persist tests count writes —
  // patches the prototype reachable from the global `Storage`, so leaving the environment's own
  // constructor in place would install storages whose prototype no spy ever touches: five tests
  // silently observing nothing while appearing to pass their setup.
  for (const name of ["Storage", "localStorage", "sessionStorage"] as const) {
    Object.defineProperty(globalThis, name, {
      value: window[name],
      configurable: true,
      writable: true,
    });
  }
}
