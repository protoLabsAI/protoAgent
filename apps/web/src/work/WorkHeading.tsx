import { Count } from "@protolabsai/ui/primitives";

export function WorkHeading({ label, count }: { label: string; count: number }) {
  return <h2 className="work-heading" aria-label={`${label}, ${count}`}>{label}<span className="work-view__announcement">, {count}</span><Count aria-hidden="true">{count}</Count></h2>;
}
