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
//   • Visibility is DECLARATIVE (`flag` / `hostOnly`) and applied at READ time by the host —
//     `visiblePaletteCommands(flagOn, onHost)`, the same two axes and the same shape settings
//     sections gate on (`visibleSections`, settings/sectionGate.ts), and the same `flag?: string`
//     contract as `registerSlashCommand`. Registration stays UNCONDITIONAL, and that is
//     load-bearing: it runs once at module load, before `/api/flags` has answered, and
//     `useFlagPredicate` fails CLOSED while that request is in flight (ADR 0068) — so a gate
//     resolved AT registration would hide a flag-gated row FOREVER. Re-filtering per render is
//     what lets the late-arriving flag flip the row on. Gates are DATA, not a predicate the
//     registry hands out: they can't throw inside the root render, can't get expensive, and
//     anyone holding the two axes can answer them (including a "why is this row hidden?" UI).
//   • `registerPaletteSource` contributes commands computed at READ time, for rows that track
//     live data (open chat tabs, a roster) rather than a fixed list. Because a source decides
//     per read WHICH rows to return, it is also the escape hatch for a condition the two gate
//     axes can't express — and a broken source is contained here (it throws, or it returns
//     something that isn't an array), where a fork-supplied predicate running inside the root
//     render would not be: an escape from this module lands on the console's ROOT error
//     boundary and replaces the whole app with the crash card.
//     READ time means the host's read, and the host has to ASK — a source's rows changing
//     bumps nothing, since the version counter only moves on register/unregister. So the two
//     halves reach the DS palette by different paths (`from`, below): statics are snapshotted
//     into `registerCommands`, while source rows are served by a DS `CommandProvider` whose
//     `getCommands(query)` the palette re-invokes on every open and every keystroke. Snapshot
//     BOTH and a source's rows freeze at whichever effect run happened to register them.
//   • `subscribePaletteCommands` + `paletteCommandsVersion` mirror the DS registry's
//     bump/subscribe shape (`createPaletteRegistry` in @protolabsai/ui), so the root view
//     `useSyncExternalStore(subscribePaletteCommands, paletteCommandsVersion)`s and picks up a
//     command registered AFTER its first render (a lazily-imported fork module, a withdrawal).
//
// Distinct from plugin manifest `palette` views (ADR 0057), which morph the palette body
// into a plugin iframe; these are trusted in-process action commands that RUN code.
import type { ReactNode } from "react";

/** What a palette command's handler receives. */
export type PaletteCommandContext = {
  /** Close the palette (call after navigating / running). */
  close: () => void;
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
  /** Muted trailing text on the row ("go to", "host instance only"). A `disabled` row says
   *  WHY here — that's what core's Fleet Room command does, so the seam needs no second
   *  "reason" field. Defaults to `keybinding`'s combo when one is named. */
  hint?: string;
  /** Id of a `registerKeybinding` binding (ADR 0063) whose shortcut this row ADVERTISES —
   *  it binds nothing; the combo still fires through the keybinding host. An id rather than
   *  a literal "⌘⇧K" because bindings are user-rebindable (Settings ▸ Keyboard persists an
   *  override), so a literal starts lying the moment the operator rebinds it: the host
   *  renders `formatCombo(effectiveCombo(binding))`, always the live combo. */
  keybinding?: string;
  /** Render the row unrunnable but still LISTED, so it stays discoverable — say why in
   *  `hint` (a mute dead row is worse than none). Contrast the gates below, which omit it. */
  disabled?: boolean;
  /** Developer-flag id (ADR 0068): listed only while the flag resolves ON — the same
   *  contract as `ClientSlashCommand.flag`. Resolved per render by the host, NEVER at
   *  registration (see the header: the fail-closed window would hide it permanently). */
  flag?: string;
  /** Host-console-only (`isHostConsole()` — the un-suffixed root or the `host` slug), the
   *  way a `hostOnly` settings section is: for rows whose target only means something there
   *  (the box-shared Global defaults). This is the URL-slug axis, NOT the fleet-nesting one
   *  (`fleetSettingsDisabledReason`) — a sister agent's slug window drives the hub's fleet
   *  and must keep fleet rows. Prefer `disabled` + `hint` when the row should stay visible
   *  and explain itself instead of vanishing. */
  hostOnly?: boolean;
  /** Invoked when the command is run. */
  run: (ctx: PaletteCommandContext) => void;
};

/** A DYNAMIC command source: called on every read, never cached, so its rows track live
 *  data. Called while the palette is being read (on open and on each keystroke), so it must
 *  be CHEAP and SYNCHRONOUS — no fetches, no store writes. A source that returns anything
 *  but an array (an `async` one returns a Promise; `false` for "nothing to show") is
 *  skipped, not trusted: see `registeredPaletteCommands`. */
export type PaletteCommandSource = () => PaletteCommand[];

/** Which half of the registry a read wants. The two halves reach the DS palette by
 *  DIFFERENT paths and so have to be readable apart: `"static"` rows are snapshotted into
 *  `registry.registerCommands` (a fixed list is correct to freeze), `"dynamic"` rows are
 *  served by a DS `CommandProvider` that is re-invoked per palette read (freezing them is
 *  the bug this split exists to prevent). `"all"` is the whole list, statics first — what a
 *  "what is registered?" reader (a test, a diagnostics view) wants. */
export type PaletteCommandOrigin = "all" | "static" | "dynamic";

const _commands = new Map<string, PaletteCommand>();
const _sources = new Set<PaletteCommandSource>();
const _listeners = new Set<() => void>();
let _version = 0;

/** Monotonic counter bumped on every register/unregister — the `useSyncExternalStore`
 *  snapshot (a number, so it's referentially stable between changes). */
export function paletteCommandsVersion(): number {
  return _version;
}

/** Whether any dynamic source is registered. The host wires the DS provider path (a
 *  debounced re-read plus the palette's "Searching…" affordance on every keystroke) only
 *  when one exists, because paying for an always-empty provider would put that spinner in
 *  front of every keystroke for nothing — in every window that mounts the palette, the
 *  frameless desktop launcher included.
 *
 *  Core ships ZERO sources, and that is a decision rather than an accident of nobody having
 *  needed one: #3292 (the chat's slash commands and the server's user-facing skills) was
 *  written as a source first and moved to the static path, because the DS keeps a provider's
 *  PREVIOUS results on screen — and runnable — for the 120ms it debounces the new query.
 *  A read-time source is right for a fork's live navigation list; it is not right for rows
 *  that RUN something. Registering/unregistering a source bumps the version, so a consumer
 *  keyed on `paletteCommandsVersion()` re-asks at the right moment. */
export function hasPaletteSources(): boolean {
  return _sources.size > 0;
}

/** Subscribe to registry changes; returns an unsubscribe fn. */
export function subscribePaletteCommands(fn: () => void): () => void {
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

/** Normalize + validate one entry, or undefined if it can't be shown. Applied to statics at
 *  registration and to a source's rows on every read (a source is re-read, so it never gets
 *  a registration-time check). Returns the entry to STORE — id-trimmed, so the dedup key and
 *  the id the palette renders can't disagree. */
function _entry(cmd: PaletteCommand | undefined): PaletteCommand | undefined {
  const id = (cmd?.id || "").trim();
  if (!cmd || !id || typeof cmd.run !== "function") return undefined;
  return cmd.id === id ? cmd : { ...cmd, id };
}

/** Register a root ⌘K command. LAST registration of an id wins (HMR-safe: a re-evaluated
 *  module replaces its own entry, and keeps its original display position). Returns an
 *  unregister fn that only removes the command if it's still the registered one — so a
 *  stale closure can't evict a newer registration of the same id. */
export function registerPaletteCommand(cmd: PaletteCommand): () => void {
  const entry = _entry(cmd);
  if (!entry) return () => {};
  _commands.set(entry.id, entry);
  bump();
  return () => {
    if (_commands.get(entry.id) !== entry) return; // superseded (or already removed) — not ours
    _commands.delete(entry.id);
    bump();
  };
}

/** Register a dynamic source of commands, re-read on every `registeredPaletteCommands()`
 *  call — which for the console's palette means every time it is opened and every keystroke
 *  typed into it, because the host serves source rows through a DS `CommandProvider` rather
 *  than a snapshot. It does NOT mean "whenever your data changes": nothing here observes a
 *  source's data, so a row that changed between two reads appears at the next read, not the
 *  instant it changed. Returns an unregister fn (idempotent).
 *
 *  KNOW WHAT THE PROVIDER PATH COSTS before putting an ACTION on it — none of it is about how
 *  live your data is:
 *    • the rows are ORDERED, never ranked against the corpus (`orderCommands` runs after
 *      `rankCommands` in palette/rootView.tsx), so they sit below every static and every
 *      surface no matter how well they match what was typed;
 *    • declaring `getCommands` at all puts a 120ms debounce and a "Searching…" spinner in
 *      front of every keystroke, in every window that mounts the palette;
 *    • and the results outlive the query they answered — the loop only overwrites them when a
 *      read RESOLVES. Our root view stamps them with their query and drops a stale stamp; the
 *      DS's own `CommandsBody` does not (protoContent#504), so a fork rendering through it
 *      gets rows that are listed, selected and runnable against a query they do not match.
 *  A source is right for a live list you BROWSE (a fork's open tabs, a roster) and for a
 *  remote search that applies the query its own way. Rows that RUN something belong on
 *  `registerPaletteCommand`, re-registered when their inputs move — which is what core's own
 *  chat rows do (`app/chatSlashPalette`, #3292). */
export function registerPaletteSource(fn: PaletteCommandSource): () => void {
  if (typeof fn !== "function") return () => {};
  _sources.add(fn);
  bump();
  return () => {
    if (_sources.delete(fn)) bump();
  };
}

/** Every registered command, UNGATED: the statics in registration order, then each dynamic
 *  source's rows (re-read now). A statically-registered id wins an id collision over a
 *  source's row, and the first source to claim an id wins over later ones — so a fork can
 *  pin one row of an otherwise generated list by registering it statically. That precedence
 *  holds for `from: "dynamic"` too: the id set is seeded from the statics either way, so the
 *  two reads never both yield the same id. `flag`/`hostOnly` are NOT applied —
 *  `visiblePaletteCommands` is the gated read a view wants. */
export function registeredPaletteCommands(from: PaletteCommandOrigin = "all"): PaletteCommand[] {
  const seen = new Set(_commands.keys());
  const out: PaletteCommand[] = from === "dynamic" ? [] : [..._commands.values()];
  if (from === "static") return out; // don't even CALL the sources — nothing would be kept
  for (const source of _sources) {
    // The whole consumption is inside the try, and the result is shape-CHECKED rather than
    // trusted: `PaletteCommandSource` types the return as an array, but a fork's module is
    // the untyped edge of a build-time seam, and the three mistakes that actually happen
    // (`async () => rows` → a Promise, an id-keyed object literal, `false` for "no rows")
    // all throw `rows is not iterable` from the `for…of`. Thrown from HERE that lands on the
    // console's root error boundary — a fork typo would blank the entire app, not one row.
    // A source that throws PART WAY through its rows keeps the rows already taken: half a
    // list beats none, and the seen-set stays consistent either way.
    try {
      const rows: unknown = source();
      if (!Array.isArray(rows)) continue;
      for (const row of rows as PaletteCommand[]) {
        const cmd = _entry(row);
        if (!cmd || seen.has(cmd.id)) continue;
        seen.add(cmd.id);
        out.push(cmd);
      }
    } catch {
      // a broken fork source must not blank the palette (or the ones after it)
    }
  }
  return out;
}

/** The commands THIS window should show, in display order: every registered command minus
 *  the ones a developer flag or the host axis gates out. `flagOn` is the host's
 *  `useFlagPredicate()`; `onHost` its `isHostConsole()`. Mirrors `visibleSections`
 *  (settings/sectionGate.ts) so the console has ONE gating vocabulary. Call it per render,
 *  not once: the flag predicate fails closed until `/api/flags` lands, and re-asking is
 *  exactly what lets a row appear when it does. `from` narrows the read to one half of the
 *  registry (see `PaletteCommandOrigin`) — the host asks for `"static"` and `"dynamic"`
 *  separately because it feeds them to the DS palette by different paths. */
export function visiblePaletteCommands(
  flagOn: (id: string) => boolean,
  onHost = true,
  from: PaletteCommandOrigin = "all",
): PaletteCommand[] {
  return registeredPaletteCommands(from).filter(
    (c) => (!c.flag || flagOn(c.flag)) && (onHost || !c.hostOnly),
  );
}
