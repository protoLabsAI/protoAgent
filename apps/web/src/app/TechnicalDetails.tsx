// The compact "Technical details" disclosure shared by the Work detail, a scheduled job's
// Recent runs, instance-wide directive details and Settings ▸ Storage (work-view.md §2.3,
// settings-storage.md §3.6). A small muted text toggle with a chevron — never a bordered
// card — closed by default, its open state owned by each mounted instance (one per run,
// never shared). The body mounts only while open, so ids and raw values stay out of the
// page (and of in-page find) until the operator asks for them.

import { ChevronRight } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import "./technical-details.css";

export function TechnicalDetails({
  children,
  label = "Technical details",
  className,
}: {
  children: ReactNode;
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <div className={["technical-details", open && "technical-details--open", className].filter(Boolean).join(" ")}>
      <button
        type="button"
        className="technical-details__toggle"
        aria-expanded={open}
        aria-controls={open ? bodyId : undefined}
        onClick={() => setOpen((was) => !was)}
      >
        <ChevronRight className="technical-details__chevron" size={14} aria-hidden />
        <span>{label}</span>
      </button>
      {open && (
        <div className="technical-details__body" id={bodyId}>
          {children}
        </div>
      )}
    </div>
  );
}
