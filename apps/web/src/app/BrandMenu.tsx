import { useRef } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";

import { Menu, MenuItem, type MenuHandle } from "@protolabsai/ui/menu";
import { Bot, ChevronDown, Palette, Server } from "lucide-react";

import { useUI } from "../state/uiStore";

// Compact settings menu anchored to the header brand mark (#1544). Click — or right-click —
// the logo to open a short list of settings deep-links; each opens the settings overlay on
// that section. The DS Menu (Radix) owns keyboard access (Enter/Space open, arrows move, Esc
// close), focus management, and outside-click dismissal.
//
// DS gap: @protolabsai/ui/app-shell `Header` exposes no onClick/hook for its brand slot, so we
// pass the logo through our own trigger button (the Header renders it inside `.pl-header__brand`)
// and anchor the menu to that button.
export function BrandMenu({ logo }: { logo: ReactNode }) {
  const openGlobalSettings = useUI((s) => s.openGlobalSettings);
  const menuRef = useRef<MenuHandle>(null);

  // Right-click opens the same menu (anchored to the mark) instead of the browser's context menu.
  const onContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
    menuRef.current?.open();
  };

  return (
    <Menu
      ref={menuRef}
      trigger={
        <button
          type="button"
          className="brand-menu-trigger"
          aria-label="Settings menu"
          data-testid="brand-menu"
          onContextMenu={onContextMenu}
        >
          {logo}
          <ChevronDown size={12} className="brand-menu-chevron" aria-hidden />
        </button>
      }
    >
      <MenuItem icon={<Bot size={14} />} onSelect={() => openGlobalSettings("identity")}>
        Agent settings
      </MenuItem>
      <MenuItem icon={<Server size={14} />} onSelect={() => openGlobalSettings("fleet")}>
        Fleet settings
      </MenuItem>
      <MenuItem icon={<Palette size={14} />} onSelect={() => openGlobalSettings("theme")}>
        Theme
      </MenuItem>
    </Menu>
  );
}
