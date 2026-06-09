import { Badge } from "@protolabsai/ui/primitives";

// Thin adapter over the DS Badge: maps the app's tone vocabulary (incl. "muted")
// onto Badge's status set. Every call site keeps passing {label, tone}; the chrome
// is the shared @protolabsai/ui Badge (token-only, on-brand).
export type StatusTone = "success" | "warning" | "error" | "muted";

const TONE_TO_STATUS = {
  success: "success",
  warning: "warning",
  error: "error",
  muted: "neutral",
} as const;

export function StatusPill({ label, tone }: { label: string; tone: StatusTone }) {
  return <Badge status={TONE_TO_STATUS[tone]}>{label}</Badge>;
}
