// Pure builder operations (S3) — separated from the component so the reorder
// semantics are unit-testable without mounting the editor.

export type BuilderStep = {
  id: string;
  subagent: string;
  prompt: string;
  dependsOn: string[];
  gate: boolean;
};

/** True when the steps form a strict linear chain: the first step has no
 * dependencies and every later step depends on exactly its predecessor. Only
 * then does visual order ENCODE execution order — and only then may a reorder
 * rewrite `depends_on`. */
export function isLinearChain(steps: BuilderStep[]): boolean {
  return steps.every((s, i) =>
    i === 0 ? s.dependsOn.length === 0 : s.dependsOn.length === 1 && s.dependsOn[0] === steps[i - 1].id,
  );
}

/** Move a step from one outline position to another. A strict linear chain is
 * re-threaded so execution order follows the new visual order; any other DAG
 * keeps its `depends_on` untouched (the reorder is presentational — the lanes
 * strip stays the truth about execution order). */
export function reorderSteps(steps: BuilderStep[], from: number, to: number): BuilderStep[] {
  if (from === to || from < 0 || to < 0 || from >= steps.length || to >= steps.length) return steps;
  const chain = isLinearChain(steps);
  const next = [...steps];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  if (!chain) return next;
  return next.map((s, i) => ({ ...s, dependsOn: i === 0 ? [] : [next[i - 1].id] }));
}

/** A step id not already taken: `base-copy`, then `base-copy2`, `base-copy3`… */
export function uniqueStepId(steps: BuilderStep[], base: string): string {
  const taken = new Set(steps.map((s) => s.id.trim()));
  const stem = `${base.trim() || "step"}-copy`;
  if (!taken.has(stem)) return stem;
  let n = 2;
  while (taken.has(`${stem}${n}`)) n += 1;
  return `${stem}${n}`;
}

/** Ids of every step that (transitively) depends on `id` — offering one of
 * these as an "after:" target would create a cycle, so the editor hides them. */
export function downstreamOf(steps: BuilderStep[], id: string): Set<string> {
  const out = new Set<string>();
  let grew = true;
  while (grew) {
    grew = false;
    for (const s of steps) {
      if (out.has(s.id)) continue;
      if (s.dependsOn.includes(id) || s.dependsOn.some((d) => out.has(d))) {
        out.add(s.id);
        grew = true;
      }
    }
  }
  return out;
}
