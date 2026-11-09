import { createContext, useContext, useEffect, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { Dialog, Tooltip } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { RefreshButton } from "./ui-kit";

// Lets a UtilityWidget's dialog BODY register an action (a reload button) into the dialog
// HEADER — so the content can drop its own panel header and let its body fill the dialog
// (no double header). No-op outside a UtilityWidget.
const UtilityHeaderCtx = createContext<(n: ReactNode) => void>(() => {});

/** Register a reload control in the enclosing UtilityWidget's dialog header. Pass a STABLE
 *  `onClick` (e.g. a `useCallback` or a TanStack `refetch`) — the effect re-registers only
 *  when `busy` flips, so an unstable handler would loop. No-op outside a UtilityWidget. */
export function useUtilityHeaderReload(onClick: () => void, busy: boolean) {
  const setAction = useContext(UtilityHeaderCtx);
  useEffect(() => {
    setAction(<RefreshButton onClick={onClick} busy={busy} />);
    return () => setAction(null);
  }, [setAction, onClick, busy]);
}

/**
 * A utility-bar WIDGET (2026-06 IA pass) — a pill in the bottom-left "widgets" cluster
 * that shows a hover info popover and opens its content in a dialog on click. The shared
 * shape behind the inbox, activity, and plugin-contributed utility views. Presentation
 * only — the host owns the badge, the hover info, and the dialog body.
 *
 * The dialog body mounts only while open (so the inbox / a plugin iframe loads on demand,
 * not on every render). The body can register a reload into the dialog header via
 * `useUtilityHeaderReload`, so it doesn't render a second (redundant) header of its own.
 */
export function UtilityWidget({
  icon,
  badge,
  label,
  info,
  dialogTitle,
  dialogWidth = "min(680px, 94vw)",
  testId,
  onOpen,
  onClose,
  onContextMenu,
  open: openProp,
  onOpenChange,
  children,
}: {
  icon: ReactNode;
  badge?: ReactNode;
  /** aria-label for the pill (and its fallback hover title when `info` is absent). */
  label: string;
  /** Hover popover content — a quick preview/info; omit for a plain pill. */
  info?: ReactNode;
  dialogTitle: string;
  dialogWidth?: string;
  testId?: string;
  onOpen?: () => void;
  onClose?: () => void;
  /** Right-click the pill — wired to the context-menu system (ADR 0036). */
  onContextMenu?: (e: ReactMouseEvent) => void;
  /** Controlled open state (Q11). When provided, the widget renders from it instead of its own
   *  state — so a notification click or an Activity link can open the dialog from outside. Pair
   *  with `onOpenChange`. Omit both and the widget stays uncontrolled, the default for every
   *  existing widget. */
  open?: boolean;
  /** Called whenever the widget wants the open state to change (pill click → true, dialog
   *  close → false). Required for the controlled case; also fires in the uncontrolled case. */
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const isControlled = openProp !== undefined;
  const open = isControlled ? openProp : uncontrolledOpen;
  const setOpen = (next: boolean) => {
    // The owner holds the truth when controlled; otherwise keep driving our own state. Either
    // way tell the owner, so a controlled parent (the Decisions store) and an uncontrolled
    // listener both hear the change.
    if (!isControlled) setUncontrolledOpen(next);
    onOpenChange?.(next);
  };
  // A body-registered dialog-header action (e.g. a reload button). Reset on close so a
  // stale action never lingers when the dialog re-opens.
  const [headerAction, setHeaderAction] = useState<ReactNode>(null);
  const pill = (
    <Button
      icon
      size="xs"
      variant="ghost"
      type="button"
      aria-label={label}
      // Every pill opens a dialog (Q12) — announce it, and whether it's open right now.
      aria-haspopup="dialog"
      aria-expanded={open}
      title={info ? undefined : label}
      data-testid={testId}
      onClick={() => {
        onOpen?.();
        setOpen(true);
      }}
      onContextMenu={onContextMenu}
    >
      {icon}
      {badge}
    </Button>
  );
  return (
    <>
      {info ? <Tooltip label={info}>{pill}</Tooltip> : pill}
      {open ? (
        <Dialog
          open
          padding="roomy"
          onClose={() => {
            setOpen(false);
            setHeaderAction(null);
            onClose?.();
          }}
          title={
            <span className="util-dialog-title">
              {dialogTitle}
              {headerAction ? <span className="util-dialog-actions">{headerAction}</span> : null}
            </span>
          }
          width={dialogWidth}
        >
          <UtilityHeaderCtx.Provider value={setHeaderAction}>{children}</UtilityHeaderCtx.Provider>
        </Dialog>
      ) : null}
    </>
  );
}
