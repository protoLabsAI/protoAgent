import { useCallback, useEffect, useMemo, useState } from "react";
import { X } from "lucide-react";
import { Alert } from "@protolabsai/ui/data";
import { Button } from "@protolabsai/ui/primitives";
import { useUI } from "../state/uiStore";

// Structured plugin SETUP GAP delivered on runtime status `setup_gaps[]` (server side:
// graph/plugins/setup_gaps.py, published by operator_api/console_handlers.py — #3395). A gap
// is "this plugin is installed and enabled but it can't do its job until you do X" — it
// renders as an actionable, dismissible banner in the same shell strip that the operational
// `warnings[]` strings use. The server ALSO projects every gap into `warnings[]` as a plain
// `Label: message` line (the legacy projection, kept indefinitely for older consumers);
// `splitRuntimeWarnings` drops those lines so a gap renders once, as its banner.
//
// Actions are CLOSED, server-sanitized DATA — never behavior. Each carries a `kind` from a
// fixed vocabulary; the console maps ONLY the host-allowlisted kinds to an existing
// UI-store method (below). A plugin string is never turned into a URL, markup, or a
// callback — anything unrecognized renders no interactive control and the banner degrades
// to a plain message. This mirrors ACTION_KINDS in setup_gaps.py on the way out.
export type SetupGapAction = {
  kind: string;
  target?: string;
  label?: string;
  fields?: string[];
};

export type SetupGap = {
  plugin: string;
  label: string;
  key: string;
  message: string;
  actions?: SetupGapAction[];
};

/** True for a well-formed structured setup gap. A malformed `setup_gaps[]` entry is dropped
 *  rather than crashing the strip (its legacy `warnings[]` line then still renders plainly). */
export function isSetupGap(value: unknown): value is SetupGap {
  if (!value || typeof value !== "object") return false;
  const g = value as Record<string, unknown>;
  return (
    typeof g.plugin === "string" &&
    typeof g.key === "string" &&
    typeof g.message === "string" &&
    typeof g.label === "string"
  );
}

/** The legacy `warnings[]` line the server projects for a gap — `setup_gaps.warnings()` in
 *  graph/plugins/setup_gaps.py (`f"{label}: {message}"`). Pinned against the real handler by
 *  tests/test_console_handlers.py::test_runtime_status_setup_gaps_match_the_console_e2e_golden,
 *  because if the two drift, every gap renders twice (banner + plain alert). */
export function gapWarningLine(gap: SetupGap): string {
  return `${gap.label}: ${gap.message}`;
}

/**
 * Split a runtime status into what the shell strip renders:
 *  - `setupGaps` — the well-formed records from `setup_gaps[]` (actionable, dismissible banners);
 *  - `plainWarnings` — the `warnings[]` strings, MINUS each structured gap's own legacy line.
 *
 * The server sends every gap twice (the record, and its `Label: message` line), so rendering
 * both would double it; rendering only `warnings[]` — what the console did until this — shows a
 * gap as a plain alert with no Configure button and no dismiss. The dedupe runs against ALL
 * structured gaps, not just the visible ones, so dismissing a banner can't resurface its line
 * as a plain alert. A server without `setup_gaps` (pre-#3395) loses nothing: with no records,
 * every line stays a plain warning. Non-string `warnings[]` entries are not a server shape and
 * are ignored.
 */
export function splitRuntimeWarnings(
  status: { warnings?: readonly unknown[] | null; setup_gaps?: readonly unknown[] | null } | null | undefined,
): { plainWarnings: string[]; setupGaps: SetupGap[] } {
  const setupGaps = Array.isArray(status?.setup_gaps) ? status.setup_gaps.filter(isSetupGap) : [];
  const gapLines = new Set(setupGaps.map(gapWarningLine));
  const plainWarnings = Array.isArray(status?.warnings)
    ? status.warnings.filter((w): w is string => typeof w === "string" && !gapLines.has(w))
    : [];
  return { plainWarnings, setupGaps };
}

/** Stable render/identity key for a gap — its (plugin, key) pair, which the server keys the
 *  gap registry on, so it's unique and survives message/action edits without a remount. */
export function gapIdentity(gap: SetupGap): string {
  return `${gap.plugin} ${gap.key}`;
}

/** Dismissal signature: identity PLUS the message/action content. A session dismissal keys
 *  off this, so it clears the moment the server changes what the gap SAYS or OFFERS — not
 *  just when its (plugin, key) identity changes. */
export function gapSignature(gap: SetupGap): string {
  return JSON.stringify([gap.plugin, gap.key, gap.message, gap.actions ?? []]);
}

const DISMISS_KEY = "protoagent.setupGapDismissals";

function readDismissed(): Set<string> {
  try {
    const raw = window.sessionStorage.getItem(DISMISS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : []);
  } catch {
    // sessionStorage can be unavailable / JSON can be corrupt — fail open (nothing dismissed).
    return new Set();
  }
}

function writeDismissed(sigs: Set<string>): void {
  try {
    window.sessionStorage.setItem(DISMISS_KEY, JSON.stringify([...sigs]));
  } catch {
    // Hardened browser contexts (private mode, disabled storage) — the dismissal is still
    // honored in-memory for this render tree; it just won't survive a reload. Never throw.
  }
}

/**
 * Session-scoped dismissal for structured setup gaps.
 *
 * A dismissal is keyed by the gap's `gapSignature` (identity + message + actions) and stored
 * in sessionStorage, so it:
 *  - hides ONLY that exact, unchanged gap for the rest of the browser session;
 *  - resets for a new browser session (sessionStorage is per-session);
 *  - resets when the server changes the gap's message or actions (the signature moves);
 *  - stops being tracked when a gap clears or changes WHILE other gaps are live (its stale
 *    signature is pruned against the live set), so the store never accumulates orphaned keys —
 *    but a transient/empty runtime status never prunes, so a reload can't resurrect a dismissed
 *    gap.
 *
 * It NEVER mutates server-side configuration and never clears the underlying blocker — it's a
 * purely client-side "I've seen this" acknowledgement.
 */
export function useSetupGapDismissals(gaps: SetupGap[]): {
  visibleGaps: SetupGap[];
  dismiss: (gap: SetupGap) => void;
} {
  const [dismissed, setDismissed] = useState<Set<string>>(readDismissed);

  const liveSignatures = useMemo(() => new Set(gaps.map(gapSignature)), [gaps]);
  // A content key so the prune effect only fires when the live gap set actually changes,
  // not on every render (the `gaps` array is rebuilt each render by the caller's filter).
  const liveKey = useMemo(() => JSON.stringify([...liveSignatures].sort()), [liveSignatures]);

  useEffect(() => {
    // Prune stale dismissals ONLY when we have live gaps to compare against. An empty live
    // set is ambiguous — it's produced just as much by a transient/null runtime status during
    // a reload or poll gap as by the server genuinely clearing every gap — so pruning on it
    // would erase a still-valid session dismissal and make an unchanged gap reappear when the
    // data returns. When gaps ARE present, any stored signature absent from them belongs to a
    // gap that truly changed or cleared, so dropping it is safe housekeeping. A leftover
    // signature from a fully-empty poll is harmless: it's session-scoped, and the same gap
    // returning unchanged should stay dismissed anyway (returning changed moves its signature).
    if (liveSignatures.size === 0) return;
    setDismissed((prev) => {
      const next = new Set([...prev].filter((sig) => liveSignatures.has(sig)));
      if (next.size === prev.size) return prev; // unchanged → keep the stable reference
      writeDismissed(next);
      return next;
    });
    // liveSignatures is derived from liveKey; depending on the string keeps this stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveKey]);

  const dismiss = useCallback((gap: SetupGap) => {
    setDismissed((prev) => {
      const sig = gapSignature(gap);
      if (prev.has(sig)) return prev;
      const next = new Set(prev);
      next.add(sig);
      writeDismissed(next);
      return next;
    });
  }, []);

  const visibleGaps = useMemo(
    () => gaps.filter((gap) => !dismissed.has(gapSignature(gap))),
    [gaps, dismissed],
  );

  return { visibleGaps, dismiss };
}

/** The CTA label for one action: the server-sanitized `label` if present, else a sensible
 *  default per kind ("Configure <Plugin>" for a plugin-config fix). */
function ctaLabel(action: SetupGapAction, gap: SetupGap): string {
  if (typeof action.label === "string" && action.label.trim()) return action.label.trim();
  if (action.kind === "plugin_config") return `Configure ${gap.label}`;
  return "Open settings";
}

/**
 * One structured setup gap, rendered as an accessible, actionable, dismissible warning
 * banner. Shared verbatim by the desktop strip and the mobile banner stack (App wires both
 * from the same split), so behavior can't drift between form factors.
 */
export function SetupGapBanner({ gap, onDismiss }: { gap: SetupGap; onDismiss: () => void }) {
  // The two host-allowlisted action kinds map to existing UI-store methods — nothing else.
  const openPluginConfig = useUI((s) => s.openPluginConfig);
  const openGlobalSettings = useUI((s) => s.openGlobalSettings);

  // A CLOSED action switch: only these kinds resolve to a handler. Unknown / future /
  // malformed kinds return null → no button renders (action safety), and the banner still
  // shows its message. A plugin-provided string never becomes a URL, markup, or callback.
  const handlerFor = useCallback(
    (action: SetupGapAction): (() => void) | null => {
      switch (action.kind) {
        case "plugin_config":
          // plugin_config is server-scoped to the REPORTING plugin's own config, so the
          // fix always opens THIS gap's plugin — never one the payload names.
          return () => openPluginConfig(gap.plugin, gap.label);
        case "global_settings":
          return () => openGlobalSettings(typeof action.target === "string" ? action.target : undefined);
        default:
          return null;
      }
    },
    [gap.plugin, gap.label, openPluginConfig, openGlobalSettings],
  );

  const rawActions = Array.isArray(gap.actions) ? gap.actions : [];
  const ctas = rawActions
    .map((action, index) => {
      if (!action || typeof action !== "object") return null;
      const onClick = handlerFor(action);
      if (!onClick) return null; // unknown/malformed → render no interactive control
      return (
        <Button key={`${action.kind}:${index}`} variant="default" size="sm" type="button" onClick={onClick}>
          {ctaLabel(action, gap)}
        </Button>
      );
    })
    .filter(Boolean);

  return (
    <Alert
      status="warning"
      className="shell-warning-banner setup-gap-banner"
      action={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {ctas}
          <Button
            icon
            variant="ghost"
            size="sm"
            type="button"
            onClick={onDismiss}
            aria-label={`Dismiss the ${gap.label} setup notice`}
            data-testid="setup-gap-dismiss"
          >
            <X size={14} aria-hidden />
          </Button>
        </div>
      }
    >
      <span>
        <strong>{gap.label}:</strong> {gap.message}
      </span>
    </Alert>
  );
}
