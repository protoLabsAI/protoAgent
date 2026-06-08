// @protoagent/plugin-ui — the versioned plugin-UI SDK (ADR 0034 D4). A `ui: react` plugin remote
// imports this to talk to the host: register context-menu items (ADR 0036), and (incrementally)
// the shared QueryClient, API/auth client, theme tokens, and shell pieces. The host shares this
// package as a federation singleton, so the registry/store below are ONE instance across the
// host and every remote — that's what makes cross-boundary registerContextMenu work.
export { registerContextMenu, resolveMenu } from "./registry";
export { openContextMenu, useContextMenuStore } from "./store";
export type {
  ContextType,
  MenuItem,
  MenuDivider,
  MenuEntry,
  MenuHelpers,
  ContextMenuRegistration,
} from "./types";
