// The seam's READ-TIME half (ADR 0061): `registerPaletteSource` rows reach the DS palette
// through a `CommandProvider`, NOT through the static snapshot.
//
// Why this file mounts React rather than testing the factory alone: the bug this guards
// against never lived in `paletteRegistry.ts` — that module always re-read its sources
// correctly. It lived in the one consumer, which called the read once per effect run and
// handed the result to `registry.registerCommands`, where the DS stores it verbatim. A row
// computed from live data froze there, so ⌘K listed a tab the operator had closed and
// omitted the one they had just opened. Only mounting the hook and interrogating the DS
// registry it returns can catch that, so that is what these tests do.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PaletteRegistry } from "@protolabsai/ui/command-palette";

import { registerPaletteSource } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { buildViews } from "../lib/viewRegistry";
import { paletteSourceProvider, usePaletteRegistry } from "./usePaletteRegistry";

// The hook takes ADR 0056's whole View facade now (`{ views, viewFor }`), not a bare array.
const EMPTY_VIEWS = buildViews({ core: [], plugins: [], ext: [] });

const SOURCE_PROVIDER = "ext-palette-sources";
const allOn = () => true;
/** The DS hands `getCommands` an AbortSignal; the seam's provider is synchronous and
 *  ignores it, but the call has to be shaped like the real one. */
const read = (registry: PaletteRegistry, query = "") => {
  const provider = registry.getProviders().find((p) => p.id === SOURCE_PROVIDER);
  const rows = provider?.getCommands?.(query, { signal: new AbortController().signal }) ?? [];
  return (rows as { id: string }[]).map((c) => c.id);
};

let root: Root | null = null;
const offs: (() => void)[] = [];

/** Mount `usePaletteRegistry` and hand back the DS registry it builds. */
async function mountRegistry(): Promise<PaletteRegistry> {
  let registry: PaletteRegistry | null = null;
  const Probe = () => {
    registry = usePaletteRegistry(EMPTY_VIEWS, []);
    return null;
  };
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  root.render(h(QueryClientProvider, { client }, h(Probe)));
  await vi.waitFor(() => expect(registry).not.toBeNull());
  // …and then for the adapter's registration EFFECT to have flushed, which is a separate
  // moment: `registry` is assigned during RENDER, while everything the adapter contributes —
  // the commands AND the source provider — is registered in effects, in the commit after.
  // Returning on the render alone hands back a registry that is briefly empty, so a case that
  // reads it synchronously passes or fails on how long the commit took: green in isolation,
  // red under a loaded parallel run, and an empty read looks like "the source returned
  // nothing" rather than like a race. Both effects run in the SAME commit, so the arrival of
  // the static commands is also the signal that the provider effect has run.
  //
  // Wait on one of the UNCONDITIONAL registrations, never on the PROVIDER: waiting for the
  // provider would make the "no source ⇒ no provider" arm below unobservable — it would hang
  // instead of asserting. `fleet-room` is registered unconditionally, so it marks the flush
  // without presupposing any provider (the case below asserts there are NONE at this point),
  // and naming an id rather than `length > 0` keeps the wait from being satisfied by whatever
  // happens to register first.
  await vi.waitFor(() =>
    expect(registry!.getStaticCommands().map((c) => c.id)).toContain("fleet-room"),
  );
  return registry!;
}

beforeEach(() => {
  // The roster poll would otherwise hit the network on every mount; hanging it keeps the
  // fleet data undefined, which is a state the hook already handles (`fleet?.agents ?? []`).
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
});

afterEach(() => {
  offs.splice(0).forEach((off) => off());
  root?.unmount();
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

const source = (fn: () => PaletteCommand[]) => {
  const off = registerPaletteSource(fn);
  offs.push(off);
  return off;
};

describe("registerPaletteSource → the DS read-time provider", () => {
  it("re-reads a source's live data on every palette read, and never snapshots it", async () => {
    let tabs = ["alpha"];
    source(() =>
      tabs.map((t) => ({ id: `probe:tab:${t}`, label: `Go to ${t}`, run: () => {} })),
    );
    const registry = await mountRegistry();

    // `mountRegistry` resolves as soon as the hook has RENDERED; the effect that wires the
    // DS provider is a passive one, so the first read has to wait for it (the withdraw test
    // below already does). Without this the very first assertion races the commit and the
    // file fails on a slower box while passing in CI.
    await vi.waitFor(() => expect(read(registry)).toContain("probe:tab:alpha"));
    // The snapshot path must NOT also carry the row: a frozen copy there would win the DS's
    // id dedup (statics are listed first) and shadow the fresh one forever.
    expect(registry.getStaticCommands().map((c) => c.id)).not.toContain("probe:tab:alpha");

    // The operator opens a tab. No registration, no re-render, nothing bumps the version —
    // exactly the case the snapshot got wrong.
    tabs = ["alpha", "beta"];
    expect(read(registry)).toContain("probe:tab:beta");

    // …and a closed tab stops being listed (and stops being runnable).
    tabs = ["beta"];
    expect(read(registry)).not.toContain("probe:tab:alpha");
  });

  it("applies the query itself, because the DS appends provider results unfiltered", async () => {
    source(() => [
      { id: "probe:one", label: "Deploy staging", keywords: ["ship"], run: () => {} },
      { id: "probe:two", label: "Open inbox", hint: "go to", run: () => {} },
    ]);
    const registry = await mountRegistry();

    expect(read(registry, "")).toEqual(["probe:one", "probe:two"]); // no query → everything
    expect(read(registry, "deploy")).toEqual(["probe:one"]);
    expect(read(registry, "SHIP")).toEqual(["probe:one"]); // keywords, case-insensitively
    expect(read(registry, "go to")).toEqual(["probe:two"]); // every term must hit, hint included
    expect(read(registry, "deploy inbox")).toEqual([]); // …so a cross-row query matches neither
  });

  it("wires the provider only once a source exists, and withdraws it again", async () => {
    // The arm that pins the host's CONDITIONAL, not just the registry's boolean. Core ships
    // no sources — an always-on provider would put the palette's "Searching…" spinner and its
    // 120ms debounce in front of every keystroke in the default console, and in the desktop
    // launcher, which can serve no source rows at all. (#3292's chat rows were nearly a
    // source; they are statics precisely so this stays true.) Delete the `hasPaletteSources()`
    // check in usePaletteRegistry and this test is what reddens.
    const registry = await mountRegistry();
    // Asserted on the WHOLE provider list, not just this id. The rule the comment above
    // states is about provider COUNT — the root view raises `loading` when ANY provider
    // declares `getCommands` (`palette/rootView.tsx` early-returns only on
    // `providers.length === 0`) — so an id-specific assertion stays green while some other
    // always-on provider reintroduces the exact spinner this guards against. That is not
    // hypothetical: the live knowledge provider was added unconditionally and this test did
    // not notice. Nothing whose capability is unproven may be registered here (this mount's
    // `fetch` hangs, so `/api/runtime/status` never answers and every capability gate must
    // read as "no").
    expect(registry.getProviders().map((p) => p.id)).toEqual([]);

    const off = source(() => [{ id: "probe:late", label: "Late", run: () => {} }]);
    // Registering bumps the seam version, which re-runs the effect that wires the provider.
    await vi.waitFor(() =>
      expect(registry.getProviders().map((p) => p.id)).toContain(SOURCE_PROVIDER),
    );
    expect(read(registry)).toEqual(["probe:late"]);

    off();
    await vi.waitFor(() =>
      expect(registry.getProviders().map((p) => p.id)).not.toContain(SOURCE_PROVIDER),
    );
  });

  it("contains a broken source instead of wedging the palette on 'Searching…'", () => {
    // A sync throw out of `getCommands` escapes into the root view's `Promise.allSettled`
    // callback as an unhandled rejection, and the view never clears its loading state. The
    // view contains that too now (rootView.tsx), but core's own provider still guards itself:
    // this is the seam's half of the contract, not a second copy of the view's.
    const provider = paletteSourceProvider(() => {
      throw new Error("flags blew up");
    }, true);
    source(() => [{ id: "probe:contained", label: "Contained", run: () => {} }]);
    expect(() =>
      provider.getCommands?.("", { signal: new AbortController().signal }),
    ).not.toThrow();
  });
});
