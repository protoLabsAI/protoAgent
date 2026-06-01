import { AlertTriangle } from "lucide-react";
import { useEffect } from "react";

// A small custom confirmation modal — used for destructive actions (deleting a
// chat session, etc.) instead of the browser's window.confirm, so an accidental
// click can't silently drop something. Click-outside or Escape cancels.

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Delete",
  danger = true,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message?: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="confirm-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onCancel}
      data-testid="confirm-dialog"
    >
      <div className="confirm-card" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-head">
          {danger ? <AlertTriangle size={16} /> : null}
          <h2>{title}</h2>
        </div>
        {message ? <p className="confirm-message">{message}</p> : null}
        <div className="confirm-actions">
          <button type="button" className="secondary-button" onClick={onCancel} data-testid="confirm-cancel">
            Cancel
          </button>
          <button
            type="button"
            className={`primary-button ${danger ? "danger" : ""}`}
            onClick={onConfirm}
            autoFocus
            data-testid="confirm-accept"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
