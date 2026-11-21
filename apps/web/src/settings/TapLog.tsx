import { Button } from "@protolabsai/ui/primitives";
import { useCallback, useEffect, useState } from "react";

import { copyText } from "../lib/clipboard";
import { clearTapLog, formatTapLog, readTapLog, type TapStep } from "../lib/noticeTrace";

/** The last notification taps recorded on THIS device, and a way to read them again. Lives
 *  apart from the view so Settings ▸ Devices can tell, while its Advanced section is still
 *  closed, whether there is a tap log to show at all. */
export function useTapLog(): { rows: TapStep[]; reload: () => void } {
  const [rows, setRows] = useState<TapStep[]>([]);
  const reload = useCallback(() => { void readTapLog().then(setRows); }, []);
  useEffect(reload, [reload]);
  return { rows, reload };
}

/** What happened to the last notification taps on THIS device. A tap on an installed phone
 *  console can't be watched from anywhere else, so this is how a wrong landing gets
 *  diagnosed. Hidden until a notification has been tapped here. `rows`/`reload` come from
 *  `useTapLog` in the parent (Settings ▸ Devices ▸ Advanced). */
export function TapLog({ rows, reload }: { rows: TapStep[]; reload: () => void }) {
  const [copied, setCopied] = useState(false);
  if (!rows.length) return null;
  return (
    <details className="devices-tap-log" data-testid="tap-log" onToggle={reload}>
      <summary>What happened to the last notification tap</summary>
      <pre>{formatTapLog(rows)}</pre>
      <Button
        type="button" variant="ghost" size="sm"
        onClick={() => void copyText(formatTapLog(rows)).then((ok) => setCopied(ok))}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => void clearTapLog().then(reload)}>
        Clear
      </Button>
    </details>
  );
}
