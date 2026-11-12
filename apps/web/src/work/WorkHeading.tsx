import type { ReactNode } from "react";
import { Count } from "@protolabsai/ui/primitives";

/** `action` is the group's "+" (New in <group>, §10.1) — only the three groups the
 *  operator may add to pass one; Needs you never does (only the agent puts things there). */
export function WorkHeading({ label, count, action }: { label: string; count: number; action?: ReactNode }) {
  return <h2 className="work-heading" aria-label={`${label}, ${count}`}>{label}<span className="work-view__announcement">, {count}</span><Count aria-hidden="true">{count}</Count>{action}</h2>;
}
