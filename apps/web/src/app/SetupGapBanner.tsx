import { useCallback, useEffect, useMemo, useState } from "react";
import { X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
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
//
// `plugin_setup` is the one kind whose button reaches the server: it POSTs
// /api/plugin-setup/<gap.plugin>/<step>, a path built from THIS gap's plugin id and the
// server-validated step identifier (never a URL from the payload), and the host runs only the
// callable that plugin registered for that step ("Download the CLI", "Install Chrome"). The
// route is core and outside /api/plugins/<id>/, so no plugin manifest can un-gate it.
export type SetupGapAction = {
  kind: string;
  target?: string;
  step?: string;
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
 *  - `plainWarnings` — the `warnings[]` strings, MINUS each structured gap's own legacy line;
 *  - `gapsKnown` — whether the status carried the server's `setup_gaps[]` list at all. A current
 *    server always sends it (`[]` when there are none), so an empty list is a REAL "no gaps"
 *    that dismissals may reset on; a missing status (not loaded yet) or a missing field (a server
 *    that predates #3395) is not — `useSetupGapDismissals` must not prune on it.
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
): { plainWarnings: string[]; setupGaps: SetupGap[]; gapsKnown: boolean } {
  const gapsKnown = Array.isArray(status?.setup_gaps);
  const setupGaps = Array.isArray(status?.setup_gaps) ? status.setup_gaps.filter(isSetupGap) : [];
  const gapLines = new Set(setupGaps.map(gapWarningLine));
  const plainWarnings = Array.isArray(status?.warnings)
    ? status.warnings.filter((w): w is string => typeof w === "string" && !gapLines.has(w))
    : [];
  return { plainWarnings, setupGaps, gapsKnown };
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

// One sessionStorage entry PER AGENT: `protoagent.setupGapDismissals:<agent>`. Runtime status is
// the focused agent's and switching agents is a full page load in the same tab, so a single
// shared entry let a dismissal on one agent hide another agent's identical gap, and let one
// agent's live set prune another's dismissals (#3438 review).
const DISMISS_KEY = "protoagent.setupGapDismissals";
const dismissKey = (scope: string) => `${DISMISS_KEY}:${scope}`;

function readDismissed(scope: string): Set<string> {
  try {
    const raw = window.sessionStorage.getItem(dismissKey(scope));
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : []);
  } catch {
    // sessionStorage can be unavailable / JSON can be corrupt — fail open (nothing dismissed).
    return new Set();
  }
}

function writeDismissed(scope: string, sigs: Set<string>): void {
  try {
    if (sigs.size) window.sessionStorage.setItem(dismissKey(scope), JSON.stringify([...sigs]));
    else window.sessionStorage.removeItem(dismissKey(scope));
  } catch {
    // Hardened browser contexts (private mode, disabled storage) — the dismissal is still
    // honored in-memory for this render tree; it just won't survive a reload. Never throw.
  }
}

/**
 * Session-scoped, per-agent dismissal for structured setup gaps.
 *
 * A dismissal is keyed by the gap's `gapSignature` (identity + message + actions) and stored in
 * sessionStorage under the focused agent (`scope` — App passes `currentSlug()`), so it:
 *  - hides ONLY that exact, unchanged gap, on THAT agent, for the rest of the browser session —
 *    another agent's identical gap is its own problem and still shows;
 *  - resets for a new browser session (sessionStorage is per-session);
 *  - resets when the server changes the gap's message or actions (the signature moves);
 *  - resets when the server CLEARS the gap: whenever the agent's gap list is known
 *    (`authoritative` — `splitRuntimeWarnings`' `gapsKnown`), stored signatures missing from it
 *    are pruned, including all of them on a real `setup_gaps: []`. So a gap the operator fixed
 *    that breaks again later shows again, instead of staying hidden until the app restarts;
 *  - never prunes while the list is UNKNOWN (status not loaded yet, or a server that predates
 *    `setup_gaps`), so a reload can't resurrect a dismissed gap.
 *
 * It NEVER mutates server-side configuration and never clears the underlying blocker — it's a
 * purely client-side "I've seen this" acknowledgement.
 */
export function useSetupGapDismissals(
  gaps: SetupGap[],
  { scope, authoritative }: { scope: string; authoritative: boolean },
): {
  visibleGaps: SetupGap[];
  dismiss: (gap: SetupGap) => void;
} {
  const [state, setState] = useState(() => ({ scope, sigs: readDismissed(scope) }));
  // A different agent → ITS dismissals, re-read during render (React's pattern for resetting
  // state on a prop change), so no frame ever filters one agent's gaps by another's set.
  let current = state;
  if (state.scope !== scope) {
    current = { scope, sigs: readDismissed(scope) };
    setState(current);
  }
  const dismissed = current.sigs;

  const liveSignatures = useMemo(() => new Set(gaps.map(gapSignature)), [gaps]);
  // A content key so the prune effect only fires when the live gap set actually changes,
  // not on every render (the `gaps` array is rebuilt each render by the caller's filter).
  const liveKey = useMemo(() => JSON.stringify([...liveSignatures].sort()), [liveSignatures]);

  useEffect(() => {
    // Prune only against a KNOWN list (see the doc above): an unknown one — no status yet, or no
    // `setup_gaps` field — says nothing about what the server cleared.
    if (!authoritative) return;
    setState((prev) => {
      if (prev.scope !== scope) return prev;
      const next = new Set([...prev.sigs].filter((sig) => liveSignatures.has(sig)));
      if (next.size === prev.sigs.size) return prev; // unchanged → keep the stable reference
      writeDismissed(scope, next);
      return { scope, sigs: next };
    });
    // liveSignatures is derived from liveKey; depending on the string keeps this stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveKey, authoritative, scope]);

  const dismiss = useCallback((gap: SetupGap) => {
    setState((prev) => {
      const sig = gapSignature(gap);
      if (prev.sigs.has(sig)) return prev;
      const next = new Set(prev.sigs);
      next.add(sig);
      writeDismissed(prev.scope, next);
      return { scope: prev.scope, sigs: next };
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
  if (action.kind === "plugin_setup") return "Run setup";
  return "Open settings";
}

type SetupStepWatch = { plugin: string; key: string; since: number; until: number };

/**
 * Whether App can stop polling runtime status for a setup step it started: the watch expired,
 * or a status fetched AFTER the click (`dataUpdatedAt > since`) shows the gap gone (done) or
 * offering a `plugin_setup` button again (failed → Retry). A status from before the click still
 * shows the button that was just pressed, so it proves nothing; an unknown list neither.
 */
export function setupStepWatchDone(
  watch: SetupStepWatch | undefined,
  gaps: SetupGap[],
  gapsKnown: boolean,
  dataUpdatedAt: number,
  now: number = Date.now(),
): boolean {
  if (!watch) return true;
  if (now >= watch.until) return true;
  if (!gapsKnown || dataUpdatedAt <= watch.since) return false;
  const gap = gaps.find((g) => g.plugin === watch.plugin && g.key === watch.key);
  if (!gap) return true;
  return (Array.isArray(gap.actions) ? gap.actions : []).some((a) => Boolean(a) && a.kind === "plugin_setup");
}

/** True while a banner-started setup step is being watched — App's status poll reads this. */
export function setupStepWatchActive(now: number = Date.now()): boolean {
  const watch = useUI.getState().setupStepWatch;
  return Boolean(watch && watch.until > now);
}

/**
 * A `plugin_setup` CTA: runs the step the REPORTING plugin registered. Its own component so the
 * query-client / toast hooks exist only where such an action is actually rendered. A `pending`
 * answer (the plugin started a download/install in the background) arms App's status poll so
 * the banner's progress and outcome show up without a reload.
 */
function SetupStepButton({ gap, action }: { gap: SetupGap; action: SetupGapAction }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const watchSetupStep = useUI((s) => s.watchSetupStep);
  const [busy, setBusy] = useState(false);
  const step = typeof action.step === "string" ? action.step : "";
  const label = ctaLabel(action, gap);

  const run = useCallback(async () => {
    if (!step) return;
    setBusy(true);
    try {
      const res = await api.runPluginSetupStep(gap.plugin, step);
      if (res.ok === false) {
        toast({ tone: "error", title: `${gap.label}: ${label} failed`, message: res.message || "The setup step failed." });
      } else {
        if (res.pending) watchSetupStep(gap.plugin, gap.key);
        if (res.message) toast({ tone: res.pending ? "info" : "success", title: gap.label, message: res.message });
      }
    } catch (err) {
      toast({ tone: "error", title: `${gap.label}: ${label} failed`, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      // The step re-reported its gap before answering ("downloading…", no button) — show it now.
      void queryClient.invalidateQueries({ queryKey: queryKeys.runtime });
    }
  }, [gap.plugin, gap.key, gap.label, label, step, toast, watchSetupStep, queryClient]);

  return (
    <Button variant="default" size="sm" type="button" disabled={busy} onClick={() => void run()}>
      {busy ? "Working…" : label}
    </Button>
  );
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
      if (action.kind === "plugin_setup") {
        // No step, no button: there'd be nothing to run (the host drops such an action anyway).
        return typeof action.step === "string" && action.step ? (
          <SetupStepButton key={`${action.kind}:${action.step}:${index}`} gap={gap} action={action} />
        ) : null;
      }
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
