// Host-side context-menu barrel. The registry/store/types now live in @protoagent/plugin-ui (the
// shared SDK singleton, ADR 0034) — re-exported here so host code keeps importing from
// "../contextMenu". The renderer + the host's own menu registrations stay local.
export {
  registerContextMenu,
  resolveMenu,
  openContextMenu,
  useContextMenuStore,
} from "@protoagent/plugin-ui";
export type {
  ContextType,
  MenuItem,
  MenuDivider,
  MenuEntry,
  MenuHelpers,
  ContextMenuRegistration,
} from "@protoagent/plugin-ui";
export { ContextMenuRenderer } from "./ContextMenuRenderer";
import "./registrations";
