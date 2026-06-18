import { Menu as MenuIcon } from "lucide-react";
import { Button } from "@protolabsai/ui/primitives";

/**
 * The header hamburger (top-right) — opens the app drawer (AppDrawer): Global settings,
 * Telemetry, GitHub/Docs links, and on mobile the surface nav. Replaces the earlier
 * dropdown-Menu version (#1137/#1155), which was pulled — same trigger, a side drawer
 * now instead of a dropdown.
 */
export function HamburgerMenu({ onOpen }: { onOpen: () => void }) {
  return (
    <Button
      icon
      variant="ghost"
      type="button"
      title="Menu"
      aria-label="Menu"
      data-testid="header-menu"
      onClick={onOpen}
    >
      <MenuIcon size={18} />
    </Button>
  );
}
