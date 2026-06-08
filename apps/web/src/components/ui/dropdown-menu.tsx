import * as React from "react";
import * as DM from "@radix-ui/react-dropdown-menu";

import { cn } from "../../lib/cn";

// shadcn/Radix DropdownMenu (ADR 0037) — owned source, themed by the @protolabsai/design tokens
// (bg-popover / text-popover-foreground / focus:bg-accent). Used by the context-menu renderer.
export const DropdownMenu = DM.Root;
export const DropdownMenuTrigger = DM.Trigger;

export const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof DM.Content>,
  React.ComponentPropsWithoutRef<typeof DM.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <DM.Portal>
    <DM.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        "z-50 min-w-[10rem] overflow-hidden rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md",
        "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        className,
      )}
      {...props}
    />
  </DM.Portal>
));
DropdownMenuContent.displayName = "DropdownMenuContent";

export const DropdownMenuItem = React.forwardRef<
  React.ElementRef<typeof DM.Item>,
  React.ComponentPropsWithoutRef<typeof DM.Item> & { danger?: boolean }
>(({ className, danger, ...props }, ref) => (
  <DM.Item
    ref={ref}
    className={cn(
      "relative flex cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none",
      "focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
      danger && "text-destructive focus:text-destructive-foreground focus:bg-destructive",
      className,
    )}
    {...props}
  />
));
DropdownMenuItem.displayName = "DropdownMenuItem";

export const DropdownMenuSeparator = React.forwardRef<
  React.ElementRef<typeof DM.Separator>,
  React.ComponentPropsWithoutRef<typeof DM.Separator>
>(({ className, ...props }, ref) => (
  <DM.Separator ref={ref} className={cn("-mx-1 my-1 h-px bg-border", className)} {...props} />
));
DropdownMenuSeparator.displayName = "DropdownMenuSeparator";

export const DropdownMenuLabel = React.forwardRef<
  React.ElementRef<typeof DM.Label>,
  React.ComponentPropsWithoutRef<typeof DM.Label>
>(({ className, ...props }, ref) => (
  <DM.Label ref={ref} className={cn("px-2 py-1.5 text-xs font-medium text-muted-foreground", className)} {...props} />
));
DropdownMenuLabel.displayName = "DropdownMenuLabel";
