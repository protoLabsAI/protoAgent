// The fork seam's public API + auto-loader (ADR 0038 D3).
//
// Core ships src/ext/ with one reference module (`workflows.tsx`, ADR 0043). A FORK adds its own
// `src/ext/<name>.tsx` modules that import
// from here and self-register (registerSurface / registerContextMenu). The glob below imports them
// eagerly at startup so they register. Upstream never edits this directory → `git pull upstream`
// never conflicts; the fork rebuilds its own app. (Trusted, in-process — see the Security & trust
// model doc: this is the fork path, NOT the sandboxed-plugin path.)
export { registerSurface, registeredSurfaces } from "./registry";
export type { ExtSurface } from "./registry";
export { registerSlashCommand, registeredSlashCommands, findSlashCommand } from "./slashRegistry";
export type { ClientSlashCommand, SlashContext } from "./slashRegistry";
export { registerComposerAction, registeredComposerActions } from "./composerRegistry";
export type { ComposerAction, ComposerActionContext } from "./composerRegistry";
export { registerChatComponent, registeredChatComponents } from "./componentRegistry";
export type { ChatComponentRenderer } from "./componentRegistry";
export { registerPaletteCommand, registeredPaletteCommands } from "./paletteRegistry";
export type { PaletteCommand, PaletteCommandContext } from "./paletteRegistry";
export { registerKeybinding, registeredKeybindings } from "./keybindingRegistry";
export type { Keybinding } from "./keybindingRegistry";
export { createUISlice, registeredUISlices } from "./uiStateRegistry";
export { registerContextMenu, openContextMenu } from "../contextMenu";
export type { MenuItem, MenuEntry, ContextType } from "../contextMenu";

// Eagerly load every fork-authored surface module so it self-registers. Empty in core.
const _fork = import.meta.glob("./*.tsx", { eager: true });
void _fork;
