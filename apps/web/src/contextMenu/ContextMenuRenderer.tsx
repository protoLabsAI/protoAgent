import { useEffect, useRef } from "react";
import { Menu, MenuItem, MenuSeparator, type MenuHandle } from "@protolabsai/ui/menu";
import { resolveMenu } from "./registry";
import { useContextMenuStore } from "./store";

// One renderer at the app root (ADR 0036 D1). The rendered primitive is now the DS
// `Menu` (@protolabsai/ui) — Radix-backed, opened at the cursor via its imperative
// `ref.open({x,y})`. The registry / `ContextType` keying / open-state store stay
// host-side (app domain); only the menu chrome is shared.
export function ContextMenuRenderer() {
  const { open, type, x, y, ctx, close } = useContextMenuStore();
  const menuRef = useRef<MenuHandle>(null);

  const entries = open ? resolveMenu(type, ctx) : [];
  const visible = entries.filter((e) =>
    "divider" in e ? true : typeof e.visible === "function" ? e.visible(ctx) : e.visible !== false,
  );
  const hasItems = visible.some((e) => !("divider" in e));

  // Bridge the host store → the DS Menu's imperative open-at-coords.
  useEffect(() => {
    if (open && hasItems) menuRef.current?.open({ x, y });
    else menuRef.current?.close();
  }, [open, hasItems, x, y]);

  return (
    <Menu ref={menuRef} align="start" onOpenChange={(o) => { if (!o) close(); }}>
      {visible.map((e) =>
        "divider" in e ? (
          <MenuSeparator key={e.id} />
        ) : (
          <MenuItem
            key={e.id}
            icon={typeof e.icon === "function" ? e.icon(ctx) : e.icon}
            destructive={e.danger}
            disabled={typeof e.disabled === "function" ? e.disabled(ctx) : e.disabled}
            onSelect={() => { void e.run(ctx, { close }); }}
          >
            {typeof e.label === "function" ? e.label(ctx) : e.label}
            {e.shortcut ? <span style={{ marginLeft: "auto", opacity: 0.6 }}>{e.shortcut}</span> : null}
          </MenuItem>
        ),
      )}
    </Menu>
  );
}
