import type { ReactNode } from "react";
import { StatusDot } from "@protolabsai/ui/data";
import { Badge, type Status } from "@protolabsai/ui/primitives";
import { statusTone, statusWord, type WorkStatus } from "./tone";
import "./work-row.css";

export type WorkRowProps = {
  status: WorkStatus;
  title: string;
  meta?: string;
  onOpen: () => void;
  /** Supplied only for the current item, for focus restoration between frames. */
  focusKey?: string;
  /** The v2 group rows name the KIND in the badge ("Decision", "Job", "Time"…, the
   *  "kind as a tag" rule, work-consolidation §2) and lead with a per-kind icon.
   *  Omitted, the row keeps the per-section status badge and dot. */
  tag?: string;
  /** The tag's badge tone; a tagged row defaults to neutral (§2: only Needs you accents). */
  tagTone?: Status;
  /** Replaces the leading `StatusDot` — e.g. a not-started job's dashed circle (§2). */
  icon?: ReactNode;
  /** Overrides the kind word the badge shows (e.g. "Instruction" / "Approval", §2) —
   *  same rule as `tag`, from the instructions slice; `tag` wins if both are passed. */
  badge?: string;
  /** Overrides the dot + badge tone when the kind isn't carried by the status (e.g. an
   *  in-force instruction reads neutral/"waiting", but the group paints it info). */
  tone?: Status;
};

export function WorkRow({ status, title, meta, onOpen, focusKey, tag, tagTone, icon, badge, tone }: WorkRowProps) {
  const dotTone = tone ?? statusTone(status);
  return (
    <button type="button" className="work-row" onClick={onOpen} data-surface-focus={focusKey}>
      {icon ? <span className="work-row__icon" aria-hidden="true">{icon}</span>
        : <StatusDot status={dotTone} pulse={status === "running"} />}
      <span className="work-row__body">
        <span className="work-row__title">{title}</span>
        {meta?.trim() && <span className="work-row__meta">{meta}</span>}
      </span>
      <Badge status={tag ? tagTone ?? "neutral" : dotTone}>{tag ?? badge ?? statusWord(status)}</Badge>
    </button>
  );
}
