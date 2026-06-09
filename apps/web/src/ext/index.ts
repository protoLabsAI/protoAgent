// The fork seam's public API + auto-loader (ADR 0038 D3).
//
// Core ships src/ext/ with no `*.tsx` files. A FORK adds `src/ext/<name>.tsx` modules that import
// from here and self-register (registerSurface / registerContextMenu). The glob below imports them
// eagerly at startup so they register. Upstream never edits this directory → `git pull upstream`
// never conflicts; the fork rebuilds its own app. (Trusted, in-process — see the Security & trust
// model doc: this is the fork path, NOT the sandboxed-plugin path.)
export { registerSurface, registeredSurfaces } from "./registry";
export type { ExtSurface } from "./registry";
export { registerContextMenu, openContextMenu } from "../contextMenu";
export type { MenuItem, MenuEntry, ContextType } from "../contextMenu";

// Eagerly load every fork-authored surface module so it self-registers. Empty in core.
const _fork = import.meta.glob("./*.tsx", { eager: true });
void _fork;
