// Snapshot review helpers (ADR 0091 Slice 1) — extracted from SnapshotPanel so the
// classification is unit-testable without mounting the component (the resumedTurn /
// streamWatchdog precedent; there are no component tests in this repo).

/** Pattern kinds that are NOT credentials. Mirrors `graph/snapshot_op.NON_CREDENTIAL_KINDS`.
 *
 *  Duplicated across the language boundary rather than shipped down the wire: the server
 *  already splits these correctly in `REVIEW.md`, and adding a field to the API purely so
 *  the console can re-derive a two-item set would be more coupling than it saves. Keep the
 *  two in step — the test below names the file to change. */
export const NON_CREDENTIAL_KINDS = new Set(["home-path"]);

export type Finding = { where: string; kinds: string[] };

/**
 * Split pattern-sweep hits by **what the operator should do about them**, not by detector.
 *
 * A scrubbed credential means "this is still live in your agent — rotate it". A scrubbed
 * home path means "nothing to rotate; re-point it on the target". Showing them under one
 * heading sends someone hunting a breach that never happened, which is why the server's
 * REVIEW.md splits them the same way.
 *
 * A single location can produce both (a config value holding a token AND a path), so each
 * side keeps only its own kinds rather than assigning the whole location to one bucket.
 */
export function splitFindings(hits: Record<string, string[]>): {
  credentials: Finding[];
  machineLocal: Finding[];
} {
  const credentials: Finding[] = [];
  const machineLocal: Finding[] = [];
  for (const where of Object.keys(hits).sort()) {
    const kinds = hits[where] ?? [];
    const cred = kinds.filter((k) => !NON_CREDENTIAL_KINDS.has(k));
    const local = kinds.filter((k) => NON_CREDENTIAL_KINDS.has(k));
    if (cred.length) credentials.push({ where, kinds: cred });
    if (local.length) machineLocal.push({ where, kinds: local });
  }
  return { credentials, machineLocal };
}
