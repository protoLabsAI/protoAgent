import { Table, TBody, Td, Th, THead, Tr } from "@protolabsai/ui/data";
import type { HitlDisplayTable } from "../lib/types";

const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

// Mirror server/hitl_display.py for legacy/unvalidated payloads. Reject a bad table
// whole: truncation could hide information relevant to the approval. Extra keys
// never become DOM/DS props, and this path never consults the component registry.
function tableData(display: unknown): HitlDisplayTable["props"] | null {
  if (!isObject(display) || display.component !== "table" || !isObject(display.props)) return null;
  const { columns, rows } = display.props;
  if (!Array.isArray(columns) || !Array.isArray(rows) || columns.length > 16 || rows.length > 100) return null;
  const boundedString = (value: unknown, max: number): value is string =>
    typeof value === "string" && value.length <= max * 2 && [...value].length <= max;
  // for..of also rejects holes in non-JSON arrays rather than skipping validation.
  for (const column of columns) if (!boundedString(column, 200)) return null;
  for (const row of rows) {
    if (!Array.isArray(row) || row.length !== columns.length) return null;
    for (const cell of row) {
      if (cell === null || typeof cell === "boolean") continue;
      if (typeof cell === "number" && Number.isFinite(cell)) continue;
      if (!boundedString(cell, 1000)) return null;
    }
  }
  const props = { columns, rows } as HitlDisplayTable["props"];
  const json = JSON.stringify({ component: "table", props });
  // Python's default JSON separators include spaces after commas and colons.
  const separatorSpaces = 6 + Math.max(0, columns.length - 1) + Math.max(0, rows.length - 1)
    + rows.length * Math.max(0, columns.length - 1);
  if (new TextEncoder().encode(json).length + separatorSpaces > 64 * 1024) return null;
  return props;
}

/** Data only: no callbacks, bridge, registry, or executable content. */
export function HitlDisplay({ display }: { display: unknown }) {
  const props = tableData(display);
  if (!props) return null;
  return (
    <div className="hitl-display" style={{ overflowX: "auto", overflowWrap: "anywhere" }}>
      <Table>
        <THead>
          <Tr>{props.columns.map((column, i) => <Th key={i} scope="col">{column}</Th>)}</Tr>
        </THead>
        <TBody>
          {props.rows.map((row, i) => (
            <Tr key={i}>
              {row.map((cell, j) => <Td key={j}>{cell === null ? "" : String(cell)}</Td>)}
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}
