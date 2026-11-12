import type { ReactNode } from "react";
import { StatusDot } from "@protolabsai/ui/data";
import { Badge } from "@protolabsai/ui/primitives";
import { statusTone, statusWord, type WorkStatus } from "./tone";
import "./work-row.css";

export type WorkRowProps = {
  status: WorkStatus;
  title: string;
  meta?: string;
  onOpen: () => void;
  /** Supplied only for the current item, for focus restoration between frames. */
  focusKey?: string;
  /** The item's kind shown in the badge instead of the status word — the "kind as a tag"
   *  rule (work-consolidation §2), e.g. "Job" or "Goal stopped". */
  tag?: string;
  /** Replaces the leading `StatusDot` — e.g. a not-started job's dashed circle (§2). */
  icon?: ReactNode;
};

export function WorkRow({ status, title, meta, onOpen, focusKey, tag, icon }: WorkRowProps) {
  return (
    <button type="button" className="work-row" onClick={onOpen} data-surface-focus={focusKey}>
      {icon ?? <StatusDot status={statusTone(status)} pulse={status === "running"} />}
      <span className="work-row__body">
        <span className="work-row__title">{title}</span>
        {meta?.trim() && <span className="work-row__meta">{meta}</span>}
      </span>
      <Badge status={statusTone(status)}>{tag ?? statusWord(status)}</Badge>
    </button>
  );
}
