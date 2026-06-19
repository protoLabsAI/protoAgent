import { useState } from "react";
import type { ReactNode } from "react";
import { Dialog, Tooltip } from "@protolabsai/ui/overlays";

/**
 * A utility-bar WIDGET (2026-06 IA pass) — a pill in the bottom-left "widgets" cluster
 * that shows a hover info popover and opens its content in a dialog on click. The shared
 * shape behind the inbox and plugin-contributed utility views. Presentation only — the
 * host owns the badge, the hover info, and the dialog body.
 *
 * The dialog body mounts only while open (so the inbox / a plugin iframe loads on demand,
 * not on every render).
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
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pill = (
    <button
      type="button"
      className="util-btn"
      aria-label={label}
      title={info ? undefined : label}
      data-testid={testId}
      onClick={() => {
        onOpen?.();
        setOpen(true);
      }}
    >
      {icon}
      {badge}
    </button>
  );
  return (
    <>
      {info ? <Tooltip label={info}>{pill}</Tooltip> : pill}
      {open ? (
        <Dialog
          open
          onClose={() => {
            setOpen(false);
            onClose?.();
          }}
          title={dialogTitle}
          width={dialogWidth}
        >
          {children}
        </Dialog>
      ) : null}
    </>
  );
}
