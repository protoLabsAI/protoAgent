import { Button } from "@protolabsai/ui/primitives";
import { Plus, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { PathPicker } from "./PathPicker";

// The list form of a path setting (`type: path` + `multiple: true`) — e.g. the data
// plugin's "Data folders". One row per path, each with its own Browse…, plus Remove and
// an "Add folder" that appends a row and opens Browse… for it straight away. Browse on
// the single picker REPLACES the whole value, which is wrong for a field holding several.
//
// The value is still ONE string, the rows joined with "\n": existing configs, older
// cores (which show the single text box) and readers that split on commas/newlines all
// keep working. Existing comma-separated values load as rows too.

/** Split a stored multi-path value into rows: newline OR comma separated, trimmed,
 *  blanks dropped. */
export function splitPathList(value: unknown): string[] {
  if (typeof value !== "string") return [];
  return value
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Join rows into the stored value: trimmed, blanks dropped, duplicates removed (first
 *  occurrence wins, order kept), "\n"-separated. */
export function joinPathList(rows: string[]): string {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of rows) {
    const p = raw.trim();
    if (!p || seen.has(p)) continue;
    seen.add(p);
    out.push(p);
  }
  return out.join("\n");
}

type Row = { id: number; path: string; browse: boolean };

export function PathListPicker({
  value,
  onChange,
  kind = "dir",
  id,
  label = "Folder",
  describedBy,
}: {
  value: string;
  onChange: (v: string) => void;
  kind?: "dir" | "file";
  id?: string;
  // The field's label — names each row's input ("Data folders 2") for screen readers.
  label?: string;
  describedBy?: string;
}) {
  const nextId = useRef(0);
  const toRows = (v: string): Row[] => splitPathList(v).map((path) => ({ id: nextId.current++, path, browse: false }));
  const [rows, setRows] = useState<Row[]>(() => toRows(value));
  // The joined string this control last emitted. A `value` that differs came from
  // OUTSIDE (Discard, reset, a reload) and re-seeds the rows; our own echo does not —
  // otherwise a just-added blank row would vanish (blanks never reach the joined value).
  const emitted = useRef(joinPathList(rows.map((r) => r.path)));
  useEffect(() => {
    if (value === emitted.current) return;
    emitted.current = joinPathList(splitPathList(value));
    if (joinPathList(rows.map((r) => r.path)) !== emitted.current) setRows(toRows(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const commit = (next: Row[]) => {
    setRows(next);
    const joined = joinPathList(next.map((r) => r.path));
    if (joined !== emitted.current) {
      emitted.current = joined;
      onChange(joined);
    }
  };

  const noun = kind === "file" ? "file" : "folder";
  return (
    <div id={id} className="path-list" role="group" aria-label={label} aria-describedby={describedBy}>
      {rows.map((row, i) => (
        <div key={row.id} className="path-list-row">
          <div className="path-list-picker">
            <PathPicker
              kind={kind}
              value={row.path}
              autoBrowse={row.browse}
              ariaLabel={`${label} ${i + 1}`}
              browseLabel={`Browse for ${noun} ${i + 1}`}
              onChange={(v) => commit(rows.map((r) => (r.id === row.id ? { ...r, path: v } : r)))}
            />
          </div>
          <Button
            variant="ghost"
            size="sm"
            type="button"
            aria-label={`Remove ${noun} ${i + 1}${row.path ? ` (${row.path})` : ""}`}
            title="Remove"
            onClick={() => commit(rows.filter((r) => r.id !== row.id))}
          >
            <X size={14} />
          </Button>
        </div>
      ))}
      {rows.length === 0 ? <p className="path-list-empty">No {noun}s yet.</p> : null}
      <div>
        <Button
          variant="ghost"
          size="sm"
          type="button"
          onClick={() => setRows((rs) => [...rs, { id: nextId.current++, path: "", browse: true }])}
        >
          <Plus size={14} /> Add {noun}
        </Button>
      </div>
    </div>
  );
}
