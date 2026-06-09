// Host-side context-menu system (ADR 0036). The registry/store/types are host-internal again —
// they only lived in @protoagent/plugin-ui to be a Module Federation singleton; federation is
// retired (ADR 0038), so there's no cross-bundle boundary to share across.
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
export { ContextMenuRenderer } from "./ContextMenuRenderer";
import "./registrations";
