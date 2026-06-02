export type StatusTone = "success" | "warning" | "error" | "muted";

export function StatusPill({ label, tone }: { label: string; tone: StatusTone }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}
