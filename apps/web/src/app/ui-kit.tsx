// Small shared UI leaves — the button/link idioms repeated verbatim across the
// surface panels. Pure presentation, no state.

import { Button, TextLink } from "@protolabsai/ui/primitives";
import { ExternalLink, RefreshCw, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";

/**
 * Ghost icon-button that shows a spinner while `busy` (DS `Button loading`). The
 * header "Refresh" in Activity / Inbox / Schedule / Telemetry / Knowledge / Playbooks.
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
    <Button icon variant="ghost" type="button" onClick={onClick} loading={busy} title={title} aria-label={title}>
      <RefreshCw size={size} />
    </Button>
  );
}

/**
 * "Test connection" button — a ShieldCheck that swaps to a spinner while
 * `pending` (DS `Button loading`). `disabled` still disables it independently.
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
    <Button type="button" onClick={onClick} loading={pending} disabled={disabled}>
      {pending ? null : <ShieldCheck size={15} />}
      {children}
    </Button>
  );
}

/** External help link with a trailing ExternalLink glyph (and safe `rel`). */
export function HelpLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <TextLink className="settings-help-link" href={href} external>
      {children} <ExternalLink size={13} />
    </TextLink>
  );
}
