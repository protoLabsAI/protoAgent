// Build-time fork seam for ROOT COMMAND-PALETTE commands (ADR 0061, extends ADR 0038 D3).
// A fork (or core) calls `registerPaletteCommand()` to add a ⌘K command in the "Commands"
// group — WITHOUT editing `usePaletteRegistry.ts`, so `git pull upstream` stays conflict-
// free. Sibling of `registerSlashCommand` / `registerSurface` / `registerKeybinding`:
// registration at module load, LAST-write-wins by id (HMR-safe: a module re-eval replaces
// its own entry instead of being ignored), and `register*` returns an unregister fn so a
// component-scoped or feature-scoped command can withdraw itself.
// usePaletteRegistry maps these onto DS palette `Command`s.
//
// Core dogfoods this: its deep-link commands (Plugins: Discover, Settings, …) register
// through this seam (see usePaletteRegistry.ts), so the registry is the only path.
//
// Three things make the seam usable for more than a static deep-link:
//
//   • `when` is a RENDER-TIME gate, not a registration-time filter. Registration happens
//     once at module load, when async state (`/api/flags`) has not landed — and
//     `useFlagPredicate` fails CLOSED while that request is in flight, so a filter applied
//     at registration would hide a flag-gated row FOREVER. Evaluating `when` on every root
//     render lets the late-arriving flag flip the row on.
//   • `registerPaletteSource` contributes commands computed at read time, for rows that
//     track live data (open chat tabs, a roster) rather than a fixed list.
//   • `subscribePaletteCommands` + `paletteCommandsVersion` mirror the DS registry's
//     bump/subscribe shape (`createPaletteRegistry` in @protolabsai/ui), so the root view
//     can `useSyncExternalStore(subscribePaletteCommands, paletteCommandsVersion)` and
//     pick up a command registered AFTER its first render.
//
// Distinct from plugin manifest `palette` views (ADR 0057), which morph the palette body
// into a plugin iframe; these are trusted in-process action commands that RUN code.
import type { ReactNode } from "react";

/** What a palette command's handler receives. */
export type PaletteCommandContext = {
  /** Close the palette (call after navigating / running). */
  close: () => void;
};

/** What a command's `when` gate is measured against. Deliberately small and
 *  serializable-ish — a gate answers "should this row exist for this window right now?",
 *  it does not reach into stores. */
export type PaletteGateContext = {
  /** Is a developer flag ON for this session (ADR 0068)? Fail-closed while `/api/flags`
   *  is in flight — which is exactly why `when` is re-evaluated per render. */
  flagOn: (id: string) => boolean;
  /** Is this the HOST console window (`isHostConsole()`, the un-suffixed root or the
   *  reserved `host` slug)? Host-only rows (box-shared Global settings) gate on it. */
  isHost: boolean;
};

export type PaletteCommand = {
  /** Stable id (dedup key). */
  id: string;
  /** Shown in the palette. */
  label: string;
  /** Palette group; defaults to "Commands". */
  group?: string;
  /** Fuzzy-match keywords. */
  keywords?: string[];
  /** Leading icon (any React node — a DS icon, an emoji, an <img>). */
  icon?: ReactNode;
  /** Secondary text beside the label ("go to", "host instance only"). */
  hint?: string;
  /** DISPLAY-ONLY shortcut string, rendered right-aligned by the root view (e.g. "⌘⇧K").
   *  It does not bind anything — a real binding goes through `registerKeybinding`
   *  (ADR 0063); this is the label that advertises it. */
  shortcut?: string;
  /** Render the row unrunnable (still listed, so it stays discoverable). */
  disabled?: boolean;
  /** Why it's disabled — shown to explain the dead row rather than leaving it mute. */
  disabledReason?: string;
  /** RENDER-TIME visibility gate. Returning false omits the row for this render only;
   *  a later render with different state (a flag that finally loaded) can bring it back.
   *  Runs on EVERY root render for EVERY registered command, so it must be CHEAP and
   *  PURE — no fetches, no store writes, no allocation-heavy work. Omitted ⇒ always shown. */
  when?: (ctx: PaletteGateContext) => boolean;
  /** Invoked when the command is run. */
  run: (ctx: PaletteCommandContext) => void;
};

/** A DYNAMIC command source: called at READ time, never cached, so its rows track live
 *  data. Must be cheap and pure for the same reason `when` must be. */
export type PaletteCommandSource = () => PaletteCommand[];

const _commands = new Map<string, PaletteCommand>();
const _sources = new Set<PaletteCommandSource>();
const _listeners = new Set<() => void>();
let _version = 0;

/** Monotonic counter bumped on every register/unregister — the `useSyncExternalStore`
 *  snapshot (a number, so it's referentially stable between changes). */
export function paletteCommandsVersion(): number {
  return _version;
}

/** Subscribe to registry changes; returns an unsubscribe fn. */
export function subscribePaletteCommands(fn: () => void): () => void {
  if (typeof fn !== "function") return () => {};
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

function bump() {
  _version += 1;
  // Snapshot: a listener may (un)subscribe while being notified.
  for (const l of [..._listeners]) l();
}

const _valid = (cmd: PaletteCommand | undefined) =>
  !!(cmd?.id || "").trim() && typeof cmd?.run === "function";

/** Register a root ⌘K command. LAST registration of an id wins (HMR-safe: a re-evaluated
 *  module replaces its own entry, and keeps its original display position). Returns an
 *  unregister fn that only removes the command if it's still the registered one — so a
 *  stale closure can't evict a newer registration of the same id. */
export function registerPaletteCommand(cmd: PaletteCommand): () => void {
  if (!_valid(cmd)) return () => {};
  const id = cmd.id.trim();
  _commands.set(id, cmd);
  bump();
  return () => {
    if (_commands.get(id) !== cmd) return; // superseded (or already removed) — not ours
    _commands.delete(id);
    bump();
  };
}

/** Register a dynamic source of commands, re-read on every `registeredPaletteCommands()`
 *  call. Returns an unregister fn (idempotent). */
export function registerPaletteSource(fn: PaletteCommandSource): () => void {
  if (typeof fn !== "function") return () => {};
  _sources.add(fn);
  bump();
  return () => {
    if (_sources.delete(fn)) bump();
  };
}

/** Every registered command: the statics in registration order, then each dynamic
 *  source's rows (re-read now). A statically-registered id wins over a source's row with
 *  the same id, and the first source to claim an id wins over later ones. `when` is NOT
 *  applied here — it's the caller's per-render gate (see `PaletteGateContext`). */
export function registeredPaletteCommands(): PaletteCommand[] {
  const out = [..._commands.values()];
  const seen = new Set(_commands.keys());
  for (const source of _sources) {
    let rows: PaletteCommand[] = [];
    try {
      rows = source() ?? [];
    } catch {
      rows = []; // a broken fork source must not blank the palette
    }
    for (const cmd of rows) {
      if (!_valid(cmd)) continue;
      const id = cmd.id.trim();
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(cmd);
    }
  }
  return out;
}
