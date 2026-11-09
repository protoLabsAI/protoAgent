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
};

export function WorkRow({ status, title, meta, onOpen, focusKey }: WorkRowProps) {
  return (
    <button type="button" className="work-row" onClick={onOpen} data-surface-focus={focusKey}>
      <StatusDot status={statusTone(status)} pulse={status === "running"} />
      <span className="work-row__body">
        <span className="work-row__title">{title}</span>
        {meta?.trim() && <span className="work-row__meta">{meta}</span>}
      </span>
      <Badge status={statusTone(status)}>{statusWord(status)}</Badge>
    </button>
  );
}
