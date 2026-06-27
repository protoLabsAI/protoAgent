import type { ReactNode } from "react";

// ADR 0036 — what was right-clicked. Open string so plugins can define their own types.
// `rail-background` = empty rail space (not an icon) → the "Hidden views" restore menu.
// `util-widget` = a plugin's util-bar pill → Configure. `chat-tab` = a chat session tab.
export type ContextType =
  | "rail-surface"
  | "rail-background"
  | "util-widget"
  | "chat-tab"
  | "chat-message"
  | "note"
  | "bead"
  | "background"
  | (string & {});

export interface MenuHelpers { close: () => void; }

export interface MenuItem {
  id: string;
  label: string | ((ctx: any) => string);
  icon?: ReactNode | ((ctx: any) => ReactNode);
  run: (ctx: any, helpers: MenuHelpers) => void | Promise<void>;
  disabled?: boolean | ((ctx: any) => boolean);
  visible?: boolean | ((ctx: any) => boolean);
  danger?: boolean;
  shortcut?: string;
}
export interface MenuDivider { id: string; divider: true; }
export type MenuEntry = MenuItem | MenuDivider;

export interface ContextMenuRegistration {
  type: ContextType;
  priority?: number;
  items: MenuEntry[] | ((ctx: any) => MenuEntry[]);
}
