import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";

import { monthGrid, todayISO } from "./dateParts";

const WD = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// A small inline month grid for one-off scheduling (#2159). No DS date-picker exists, so
// this is hand-rolled — but deliberately minimal: month nav + click a day. Pure layout math
// lives in dateParts (tested); this is just the view. `selected` is "YYYY-MM-DD" (or "").
export function MonthCalendar({
  selected,
  onSelect,
  today = todayISO(new Date()),
}: {
  selected: string;
  onSelect: (iso: string) => void;
  today?: string;
}) {
  // The month in view — seeded from the selection, else today. State is [year, month0].
  const seed = (selected || today).split("-");
  const [view, setView] = useState<[number, number]>([Number(seed[0]), Number(seed[1]) - 1]);
  const [y, m] = view;
  const cells = monthGrid(y, m);
  const step = (delta: number) => {
    const d = new Date(y, m + delta, 1);
    setView([d.getFullYear(), d.getMonth()]);
  };

  return (
    <div className="cal" role="group" aria-label="Choose a date">
      <div className="cal-head">
        <button type="button" className="cal-nav" onClick={() => step(-1)} aria-label="Previous month"><ChevronLeft size={15} /></button>
        <span className="cal-title">{MONTHS[m]} {y}</span>
        <button type="button" className="cal-nav" onClick={() => step(1)} aria-label="Next month"><ChevronRight size={15} /></button>
      </div>
      <div className="cal-grid cal-dow">{WD.map((d) => <span key={d} className="cal-wd">{d}</span>)}</div>
      <div className="cal-grid">
        {cells.map((c) => {
          const cls = ["cal-day"];
          if (!c.inMonth) cls.push("cal-out");
          if (c.iso === selected) cls.push("cal-sel");
          if (c.iso === today) cls.push("cal-today");
          return (
            <button
              key={c.iso}
              type="button"
              className={cls.join(" ")}
              aria-pressed={c.iso === selected}
              aria-label={c.iso}
              onClick={() => onSelect(c.iso)}
            >
              {c.day}
            </button>
          );
        })}
      </div>
    </div>
  );
}
