// Pure builder operations (S3) — separated from the component so the reorder
// semantics are unit-testable without mounting the editor.

export type BuilderStep = {
  id: string;
  subagent: string;
  prompt: string;
  dependsOn: string[];
  gate: boolean;
  /** Canvas position, persisted on the step's unmanaged `ui` key. */
  ui?: { x: number; y: number };
};

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

/** Ids of every step `id` (transitively) depends on — the ancestors whose
 * outputs are actually resolved by the time this step runs. The insert-variable
 * menu offers only these (a non-upstream `{{steps.x.output}}` renders empty). */
export function upstreamOf(steps: BuilderStep[], id: string): Set<string> {
  const byId = new Map(steps.map((s) => [s.id.trim(), s]));
  const out = new Set<string>();
  const queue = [...(byId.get(id)?.dependsOn ?? [])];
  while (queue.length) {
    const dep = queue.pop()!;
    if (out.has(dep)) continue;
    out.add(dep);
    queue.push(...(byId.get(dep)?.dependsOn ?? []));
  }
  return out;
}
