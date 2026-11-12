import type { ReactNode } from "react";
import { StatusDot } from "@protolabsai/ui/data";
import type { Status } from "@protolabsai/ui/primitives";
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
  /** The v2 group rows name the KIND in the badge ("Decision", "Job", "Time"…) and lead
   *  with a per-kind icon. Omitted, the row keeps the per-section status badge and dot. */
  tag?: string;
  tagTone?: Status;
  icon?: ReactNode;
};

export function WorkRow({ status, title, meta, onOpen, focusKey, tag, tagTone, icon }: WorkRowProps) {
  return (
    <button type="button" className="work-row" onClick={onOpen} data-surface-focus={focusKey}>
      {icon ? <span className="work-row__icon" aria-hidden="true">{icon}</span>
        : <StatusDot status={statusTone(status)} pulse={status === "running"} />}
      <span className="work-row__body">
        <span className="work-row__title">{title}</span>
        {meta?.trim() && <span className="work-row__meta">{meta}</span>}
      </span>
      <Badge status={tag ? tagTone ?? "neutral" : statusTone(status)}>{tag ?? statusWord(status)}</Badge>
    </button>
  );
}
