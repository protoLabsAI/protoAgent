import { useRef } from "react";
import { Menu as MenuIcon, Settings2 } from "lucide-react";
import { Menu, MenuItem, type MenuHandle } from "@protolabsai/ui/menu";
import { Button } from "@protolabsai/ui/primitives";

/**
 * Always-on header menu (the hamburger). A ghost button that drops the DS Menu anchored
 * under it. This is the new home for global actions as Settings get rearranged app-wide
 * (ADR 0048 follow-up) — for now it carries the one-stop-shop Settings overlay (the job
 * the topbar gear used to do); more items land here as the IA settles.
 */
export function HamburgerMenu({ onOpenSettings }: { onOpenSettings: () => void }) {
  const menuRef = useRef<MenuHandle>(null);
  return (
    <>
      <Button
        icon
        variant="ghost"
        type="button"
        title="Menu"
        aria-label="Menu"
        data-testid="header-menu"
        onClick={(e) => {
          const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
          menuRef.current?.open({ x: r.right, y: r.bottom + 4 });
        }}
      >
        <MenuIcon size={18} />
      </Button>
      <Menu ref={menuRef} align="end">
        <MenuItem icon={<Settings2 size={14} />} onSelect={onOpenSettings}>
          Settings
        </MenuItem>
      </Menu>
    </>
  );
}
