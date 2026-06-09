import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { resolveMenu } from "./registry";
import { useContextMenuStore } from "./store";

// One renderer at the app root (ADR 0036 D1). A shadcn Radix DropdownMenu opened at the cursor via
// an invisible fixed-positioned trigger — so any element can summon it without wrapping targets.
export function ContextMenuRenderer() {
  const { open, type, x, y, ctx, close } = useContextMenuStore();
  const entries = open ? resolveMenu(type, ctx) : [];
  const visible = entries.filter((e) =>
    "divider" in e ? true : typeof e.visible === "function" ? e.visible(ctx) : e.visible !== false,
  );
  if (!open || visible.length === 0) return null;
  return (
    <DropdownMenu open={open} onOpenChange={(o) => { if (!o) close(); }}>
      <DropdownMenuTrigger asChild>
        <span aria-hidden style={{ position: "fixed", left: x, top: y, width: 0, height: 0 }} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" data-testid="context-menu">
        {visible.map((e) =>
          "divider" in e ? (
            <DropdownMenuSeparator key={e.id} />
          ) : (
            <DropdownMenuItem
              key={e.id}
              danger={e.danger}
              disabled={typeof e.disabled === "function" ? e.disabled(ctx) : e.disabled}
              onSelect={() => { void e.run(ctx, { close }); }}
            >
              {typeof e.icon === "function" ? e.icon(ctx) : e.icon}
              <span>{typeof e.label === "function" ? e.label(ctx) : e.label}</span>
              {e.shortcut ? <span className="ml-auto text-xs text-muted-foreground">{e.shortcut}</span> : null}
            </DropdownMenuItem>
          ),
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
