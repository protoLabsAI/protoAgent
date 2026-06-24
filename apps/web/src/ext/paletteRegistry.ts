// Build-time fork seam for ROOT COMMAND-PALETTE commands (ADR 0061, extends ADR 0038 D3).
// A fork (or core) calls `registerPaletteCommand()` to add a ⌘K command in the "Commands"
// group — WITHOUT editing `usePaletteRegistry.ts`, so `git pull upstream` stays conflict-
// free. Sibling of `registerSlashCommand` / `registerSurface`: static registration at module
// load, first-wins (HMR-safe). usePaletteRegistry maps these onto DS palette `Command`s.
//
// Core dogfoods this: its deep-link commands (Plugins: Discover, Settings, …) register
// through this seam (see usePaletteRegistry.ts), so the registry is the only path.
//
// Distinct from plugin manifest `palette` views (ADR 0057), which morph the palette body
// into a plugin iframe; these are trusted in-process action commands that RUN code.

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
  /** Invoked when the command is run. */
  run: (ctx: PaletteCommandContext) => void;
};

const _commands: PaletteCommand[] = [];

/** Register a root ⌘K command. First registration of an id wins (HMR-safe). */
export function registerPaletteCommand(cmd: PaletteCommand): void {
  const id = (cmd?.id || "").trim();
  if (!id || typeof cmd.run !== "function") return;
  if (_commands.some((c) => c.id === id)) return; // first wins
  _commands.push(cmd);
}

export function registeredPaletteCommands(): PaletteCommand[] {
  return _commands;
}
