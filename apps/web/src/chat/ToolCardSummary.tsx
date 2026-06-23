import { useState, type ReactNode } from "react";

/**
 * TEMPORARY local mirror of the proposed `@protolabsai/ui` `ToolCardSummary`
 * (protoContent#292). Folds a run of SETTLED tool cards into one expandable chip
 * ("6 tools" / "6 tools · 1 failed") so the active card stays prominent. The API + DS
 * class names match the upstream primitive 1:1, so once `@protolabsai/ui` releases it
 * this file is deleted and the import swaps to `@protolabsai/ui/tool-card`.
 */
export function ToolCardSummary({
  count,
  label = "tools",
  status = "done",
  failedCount,
  defaultOpen = false,
  children,
}: {
  count: number;
  label?: string;
  status?: "done" | "error";
  failedCount?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const text = failedCount ? `${count} ${label} · ${failedCount} failed` : `${count} ${label}`;
  return (
    <div className={`pl-toolcard-summary pl-toolcard-summary--${status}`}>
      <button
        type="button"
        className="pl-toolcard-summary__head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span
          className={`pl-toolcard__caret${open ? " pl-toolcard__caret--open" : ""}`}
          aria-hidden
        >
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 6l6 6-6 6" />
          </svg>
        </span>
        <span className={`pl-toolcard__status pl-toolcard__status--${status}`} aria-hidden>
          {status === "error" ? (
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20 6L9 17l-5-5" />
            </svg>
          )}
        </span>
        <span className="pl-toolcard-summary__text">{text}</span>
      </button>
      {open && <div className="pl-toolcard-summary__body">{children}</div>}
    </div>
  );
}
