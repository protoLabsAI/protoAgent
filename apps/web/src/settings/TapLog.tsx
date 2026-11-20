import { Button } from "@protolabsai/ui/primitives";
import { useCallback, useEffect, useState } from "react";

import { copyText } from "../lib/clipboard";
import { clearTapLog, formatTapLog, readTapLog, type TapStep } from "../lib/noticeTrace";

/** What happened to the last notification taps on THIS device. A tap on an installed phone
 *  console can't be watched from anywhere else, so this is how a wrong landing gets
 *  diagnosed. Hidden until a notification has been tapped here. */
export function TapLog() {
  const [copied, setCopied] = useState(false);
  const [rows, setRows] = useState<TapStep[]>([]);
  const load = useCallback(() => { void readTapLog().then(setRows); }, []);
  useEffect(load, [load]);
  if (!rows.length) return null;
  return (
    <details className="devices-tap-log" data-testid="tap-log" onToggle={load}>
      <summary>What happened to the last notification tap</summary>
      <pre>{formatTapLog(rows)}</pre>
      <Button
        type="button" variant="ghost" size="sm"
        onClick={() => void copyText(formatTapLog(rows)).then((ok) => setCopied(ok))}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => void clearTapLog().then(load)}>
        Clear
      </Button>
    </details>
  );
}
