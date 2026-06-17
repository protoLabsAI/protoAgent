// Small shared UI leaves — the button/link idioms repeated verbatim across the
// surface panels. Pure presentation, no state.

import { Button } from "@protolabsai/ui/primitives";
import { ExternalLink, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";

/**
 * Ghost icon-button whose glyph spins while `busy`. The header "Refresh" in
 * Activity / Inbox / Schedule / Telemetry / Knowledge / Playbooks / Commons.
 */
export function RefreshButton({
  onClick,
  busy = false,
  title = "Refresh",
  size = 16,
}: {
  onClick: () => void;
  busy?: boolean;
  title?: string;
  size?: number;
}) {
  return (
    <Button icon variant="ghost" type="button" onClick={onClick} disabled={busy} title={title} aria-label={title}>
      <RefreshCw size={size} className={busy ? "spin" : undefined} />
    </Button>
  );
}

/**
 * "Test connection" button — a ShieldCheck that swaps to a spinner while
 * `pending`. `disabled` is OR-ed with `pending` (a pending test is also disabled).
 */
export function TestConnectionButton({
  onClick,
  pending = false,
  disabled = false,
  children = "Test connection",
}: {
  onClick: () => void;
  pending?: boolean;
  disabled?: boolean;
  children?: ReactNode;
}) {
  return (
    <Button type="button" onClick={onClick} disabled={disabled || pending}>
      {pending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
      {children}
    </Button>
  );
}

/** External help link with a trailing ExternalLink glyph (and safe `rel`). */
export function HelpLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="settings-help-link" href={href} target="_blank" rel="noreferrer">
      {children} <ExternalLink size={13} />
    </a>
  );
}
