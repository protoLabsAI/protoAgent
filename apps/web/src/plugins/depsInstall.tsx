import { Alert } from "@protolabsai/ui/data";
import { Dialog } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { useCallback, useRef, useState, type ReactNode } from "react";

import { api, ApiError } from "../lib/api";
import { errMsg } from "../lib/format";
import type { PluginDepsNeeded } from "../lib/types";
import { useTrustAck } from "./TrustAckDialog";
import { usePluginRefresh } from "./usePluginManage";

// Installing a plugin's declared Python packages from the console, OUTSIDE the Plugins row:
// the setup-gap banner's "Install dependencies" button and the install-time "install them
// now?" dialog. Both go through the ONE existing route, POST /api/plugins/install-deps (the
// Plugins row's Install deps) — its source-trust re-check, its one-install-at-a-time hold
// and its marker-aware pre-check included. There is no second install path.
//
// pip install runs the packages' own build/install code, so nothing here installs without an
// explicit click: the dialog lists the exact specs and the plugin's source first, and the
// banner button is itself the operator's act (its message names the packages).

export type DepsInstallResponse = Awaited<ReturnType<typeof api.installPluginDeps>>;

/** What one install-deps call came to, in the terms the UI shows. */
export type DepsInstallOutcome =
  | { kind: "installed"; installed: string[] }
  | { kind: "partial"; installed: string[]; failed: string[] }
  | { kind: "failed"; message: string }
  | { kind: "busy"; message: string }
  | { kind: "cancelled" };

/** Map a 200 install-deps answer to an outcome. Optional deps fail SOFT server-side, so an
 *  empty `installed` isn't proof of success (#3450): `failed` names what didn't land, and
 *  `ok: false` means nothing did. */
export function depsInstallOutcome(res: DepsInstallResponse): DepsInstallOutcome {
  const installed = res.installed ?? [];
  const failed = res.failed ?? [];
  if (res.ok === false) {
    return {
      kind: "failed",
      message: `${failed.join(", ") || "The packages"} didn't install — check the server log (no pip, or the package index is unreachable).`,
    };
  }
  if (failed.length) return { kind: "partial", installed, failed };
  return { kind: "installed", installed };
}

/** Map a thrown install-deps error. A 409 is "another install is running" (one pip per
 *  environment at a time) — not a failure. Anything else carries the server's detail, which
 *  for a pip failure is pip's own error summary ("pip install failed: …"). */
export function depsInstallErrorOutcome(err: unknown): DepsInstallOutcome {
  if (err instanceof ApiError && err.status === 409) return { kind: "busy", message: err.message };
  return { kind: "failed", message: errMsg(err) };
}

/** The toast for an outcome (the banner's feedback; the dialog shows the same inline). */
export function depsInstallToast(
  name: string,
  outcome: DepsInstallOutcome,
): { tone: "success" | "info" | "error"; title: string; message: string } | null {
  switch (outcome.kind) {
    case "installed":
      return {
        tone: "success",
        title: outcome.installed.length ? "Dependencies installed" : "Dependencies already installed",
        message: `${name}: ${outcome.installed.join(", ") || "nothing new to install"}.`,
      };
    case "partial":
      return {
        tone: "info",
        title: "Some dependencies didn't install",
        message: `${name}: ${outcome.failed.join(", ")} failed — the plugin runs without them.`,
      };
    case "busy":
      return { tone: "info", title: "A dependency install is already running", message: `${name}: ${outcome.message}` };
    case "failed":
      return { tone: "error", title: "Dependencies didn't install", message: `${name}: ${outcome.message}` };
    case "cancelled":
      return null;
  }
}

/**
 * Run POST /api/plugins/install-deps for one plugin and resolve to an outcome. A `needs_ack`
 * answer (the plugin's source isn't trusted — nothing was pip'd) opens the shared trust
 * dialog; confirming acks and retries, cancelling resolves `cancelled`. Render `ackDialog`.
 * Every settled call refreshes the installed-plugin state, so a cleared deps gap (the banner,
 * the row's Install deps) goes away without a reload.
 */
export function useDepsInstaller(): {
  run: (id: string) => Promise<DepsInstallOutcome>;
  ackDialog: ReactNode;
} {
  const refreshAll = usePluginRefresh();
  const cancelRef = useRef<(() => void) | null>(null);
  const { requestAck, ackDialog } = useTrustAck({
    onAckError: () => cancelRef.current?.(),
    onCancel: () => cancelRef.current?.(),
  });

  const run = useCallback(
    (id: string): Promise<DepsInstallOutcome> =>
      new Promise((resolve) => {
        const attempt = async () => {
          let outcome: DepsInstallOutcome;
          try {
            const res = await api.installPluginDeps(id);
            if (res.needs_ack) {
              cancelRef.current = () => resolve({ kind: "cancelled" });
              requestAck({ url: res.source ?? id, source: res.source ?? id, retry: () => void attempt() });
              return;
            }
            outcome = depsInstallOutcome(res);
          } catch (err) {
            outcome = depsInstallErrorOutcome(err);
          }
          refreshAll();
          resolve(outcome);
        };
        void attempt();
      }),
    [refreshAll, requestAck],
  );

  return { run, ackDialog };
}

type Phase = { kind: "confirm" } | { kind: "installing" } | { kind: "done"; outcome: DepsInstallOutcome };

/**
 * "<Plugin> needs these Python packages — install them now?" Shown once, right after an
 * install whose response says packages are missing HERE (`deps_needed`). Lists the exact specs
 * pip will be handed and where the code came from — the operator is consenting to both — and
 * the environment they land in (this server's Python, or the desktop's managed runtime).
 * "Not now" leaves the plugin installed; its banner keeps the same Install dependencies action.
 */
export function DepsInstallDialog({ need, onClose }: { need: PluginDepsNeeded; onClose: () => void }) {
  const { run, ackDialog } = useDepsInstaller();
  const [phase, setPhase] = useState<Phase>({ kind: "confirm" });
  const hard = need.deps.filter((d) => !d.optional);
  const soft = need.deps.filter((d) => d.optional);

  const install = async () => {
    setPhase({ kind: "installing" });
    const outcome = await run(need.id);
    setPhase(outcome.kind === "cancelled" ? { kind: "confirm" } : { kind: "done", outcome });
  };

  const outcome = phase.kind === "done" ? phase.outcome : null;
  const succeeded = outcome?.kind === "installed" || outcome?.kind === "partial";
  const footer =
    phase.kind === "confirm" ? (
      <>
        <Button onClick={onClose}>Not now</Button>
        <Button variant="primary" onClick={() => void install()}>
          Install packages
        </Button>
      </>
    ) : phase.kind === "installing" ? (
      <Button variant="primary" loading disabled>
        Installing…
      </Button>
    ) : succeeded ? (
      <Button variant="primary" onClick={onClose}>
        Done
      </Button>
    ) : (
      <>
        <Button onClick={onClose}>Close</Button>
        <Button variant="primary" onClick={() => void install()}>
          Retry
        </Button>
      </>
    );

  return (
    <>
      <Dialog
        open
        onClose={phase.kind === "installing" ? undefined : onClose}
        title={`Install Python packages for ${need.name}?`}
        width="min(560px, 94vw)"
        footer={footer}
        className="plugin-deps-dialog"
      >
        <div data-testid="plugin-deps-dialog">
          <p>
            <strong>{need.name}</strong> needs {hard.length === 1 ? "this Python package" : "these Python packages"} to run:
          </p>
          <ul className="plugin-deps-list" aria-label="required packages">
            {hard.map((d) => (
              <li key={d.spec}>
                <code>{d.spec}</code>
              </li>
            ))}
          </ul>
          {soft.length ? (
            <>
              <p className="muted">Also installed, best-effort — it runs without them:</p>
              <ul className="plugin-deps-list" aria-label="optional packages">
                {soft.map((d) => (
                  <li key={d.spec}>
                    <code>{d.spec}</code>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          <p className="muted">
            Plugin source: <code>{need.source || "unknown (no recorded origin)"}</code>. pip installs them into{" "}
            {need.target || "this server's Python environment"}, running each package&apos;s own install code — only
            continue if you trust this plugin.
          </p>
          {phase.kind === "confirm" ? (
            <p className="muted">Not now leaves {need.name} installed; its warning banner offers this install again.</p>
          ) : null}
          {phase.kind === "installing" ? (
            <p role="status" className="plugin-deps-progress">
              Installing {hard.map((d) => d.name).join(", ")}…
            </p>
          ) : null}
          {outcome ? <DepsOutcomeAlert name={need.name} outcome={outcome} /> : null}
        </div>
      </Dialog>
      {ackDialog}
    </>
  );
}

function DepsOutcomeAlert({ name, outcome }: { name: string; outcome: DepsInstallOutcome }) {
  const t = depsInstallToast(name, outcome);
  if (!t) return null;
  const status = t.tone === "success" ? "success" : t.tone === "error" ? "error" : "info";
  return (
    <Alert status={status} title={t.title}>
      <span className="plugin-deps-outcome">{t.message}</span>
    </Alert>
  );
}

/**
 * The install-time prompt: call `prompt(res.deps_needed)` after a successful install and
 * render `dialog`. Only plugins with a missing REQUIRED package are asked about (an
 * optional-only gap doesn't stop the plugin running — the toast mentions it instead); several
 * (a bundle) are asked one at a time.
 */
export function useDepsPrompt(): { prompt: (needed: PluginDepsNeeded[] | undefined) => boolean; dialog: ReactNode } {
  const [queue, setQueue] = useState<PluginDepsNeeded[]>([]);
  const prompt = useCallback((needed: PluginDepsNeeded[] | undefined) => {
    const ask = needsPrompt(needed);
    if (ask.length) setQueue((q) => [...q, ...ask.filter((n) => !q.some((x) => x.id === n.id))]);
    return ask.length > 0;
  }, []);
  const current = queue[0];
  const dialog = current ? (
    <DepsInstallDialog key={current.id} need={current} onClose={() => setQueue((q) => q.slice(1))} />
  ) : null;
  return { prompt, dialog };
}

/** The `deps_needed` entries worth asking about: at least one missing required package. */
export function needsPrompt(needed: PluginDepsNeeded[] | undefined): PluginDepsNeeded[] {
  return (needed ?? []).filter((n) => n && Array.isArray(n.deps) && n.deps.some((d) => !d.optional));
}

/** Clean names of missing OPTIONAL packages across `deps_needed` (for the install toast). */
export function optionalOnlyNames(needed: PluginDepsNeeded[] | undefined): string[] {
  return (needed ?? [])
    .filter((n) => n && Array.isArray(n.deps) && !n.deps.some((d) => !d.optional))
    .flatMap((n) => n.deps.map((d) => d.name));
}
