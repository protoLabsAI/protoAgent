// Keybinding system (ADR 0063): a scoped, user-rebindable keyboard layer. Forks/plugins
// register via `registerKeybinding` (src/ext/keybindingRegistry); the global host resolves
// the focused scope + user overrides and runs the match. Settings ▸ Keyboard rebinds them.
export { useGlobalKeybindings } from "./useKeybindings";
export { useKbIntents } from "./intents";
export { useKeybindingOverrides, effectiveCombo } from "./overrides";
export { eventToCombo, formatCombo, isEditableTarget } from "./combo";
export { registerKeybinding, registeredKeybindings } from "../ext/keybindingRegistry";
export type { Keybinding } from "../ext/keybindingRegistry";
import "./coreKeybindings"; // register the core defaults (side effect)
