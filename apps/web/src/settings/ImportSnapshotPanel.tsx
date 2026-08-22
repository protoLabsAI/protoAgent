import { Spinner } from "@protolabsai/ui/data";
import { Input, SecretInput } from "@protolabsai/ui/forms";
import { useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, KeyRound, Package, ShieldAlert, Upload } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { importButtonLabel, neededSecrets, planSummary } from "./importPlan";
import "./snapshot.css";

import type { SnapshotImportPlan, SnapshotImportResult } from "../lib/types";

const NAME_RE = /^[A-Za-z0-9-_]+$/;

/**
 * Duplicate an agent from a snapshot (ADR 0091 D3, #2106) — the console half of
 * `protoagent agent import`.
 *
 * **The order is the point.** Pick a file → see the PLAN → consent → create. Applying a
 * snapshot clones the plugin repos it names and enables their code in-process, and the file
 * came from somewhere else, so the button that does it is never the first thing you can
 * press. The plan arrives from a no-side-effects inspect call; nothing is written until the
 * operator presses a button whose label says what it will run.
 *
 * Capability-granting config (`filesystem.allow_run`, `operator.allowed_dirs`, MCP servers)
 * is shown rather than stripped — it is part of the agent's definition, and neutering it
 * would produce a duplicate that behaves differently for reasons this panel couldn't
 * enumerate (ADR 0071 D1: trust, not sandbox).
 */
export function ImportSnapshotPanel({ onDone }: { onDone?: (name: string, id: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<SnapshotImportPlan | null>(null);
  const [name, setName] = useState("");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"" | "planning" | "importing">("");
  const [error, setError] = useState("");
  // An applied-but-incomplete import (missing secrets / failed plugins). Held here so its
  // detail renders IN the page: onDone is a full-page navigation into the new agent now
  // (ADR 0042 slug routing), which would unload this tree — and any toast with it — before
  // the operator could read which secrets are still needed.
  const [needsSetup, setNeedsSetup] = useState<SnapshotImportResult | null>(null);

  async function pickFile(f: File | null) {
    setFile(f);
    setPlan(null);
    setError("");
    setSecrets({});
    if (!f) return;
    setBusy("planning");
    try {
      const p = await api.snapshotPlan(f);
      setPlan(p);
      setName(p.agent_name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  async function doImport() {
    if (!file || !plan) return;
    setBusy("importing");
    try {
      const res = await api.snapshotImport(file, { name: name.trim(), secrets });
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      if (res.complete) {
        toast({ tone: "success", title: "Agent imported", message: `${res.name} is ready.` });
        onDone?.(res.name, res.workspace_id);
      } else {
        // Not a failure — ADR 0091 D3's "incomplete until its credentials are provided".
        // Saying "ready" here would send the operator to a agent that can't reach its
        // gateway. And it must NOT navigate yet either: onDone is a full page load into
        // the duplicate, which would unload this toast before it renders — so the
        // incomplete path stays here, on the durable summary below, and the operator
        // moves into the agent with its Open button once the detail has been read.
        toast({
          tone: "info",
          title: `${res.name} imported — needs setup`,
          message: res.missing_secrets.length
            ? `Still needs: ${res.missing_secrets.join(", ")}`
            : `${res.failed.length} plugin(s) failed to install.`,
        });
        setNeedsSetup(res);
      }
    } catch (e) {
      toast({
        tone: "error",
        title: "Import failed",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  }

  const needed = neededSecrets(plan);
  const nameOk = NAME_RE.test(name.trim());

  // The import has been APPLIED but the duplicate isn't ready — show what's still needed
  // where it can be read at leisure (a toast auto-dismisses; a navigation would unload it),
  // then let the operator open the agent deliberately. The form is gone: the snapshot has
  // already been consumed, pressing Install again would double-create.
  if (needsSetup) {
    return (
      <div className="snapshot-import">
        <section className="snapshot-section">
          <h3>
            <KeyRound size={14} /> {needsSetup.name} imported — needs setup
          </h3>
          {needsSetup.missing_secrets.length ? (
            <>
              <p className="snapshot-hint">
                Still needs these credentials (add them in its Settings ▸ Secrets — until
                then it can&apos;t reach its gateway):
              </p>
              <ul className="snapshot-list">
                {needsSetup.missing_secrets.map((s) => (
                  <li key={s} className="snapshot-row">
                    <code>{s}</code>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {needsSetup.failed.length ? (
            <>
              <p className="snapshot-hint">
                {needsSetup.failed.length} plugin(s) failed to install:
              </p>
              <ul className="snapshot-list">
                {needsSetup.failed.map((f) => (
                  <li key={f.id} className="snapshot-row">
                    <code>{f.id}</code>
                    <span className="snapshot-kinds">{f.error}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
        <div className="snapshot-import-actions">
          <Button onClick={() => onDone?.(needsSetup.name, needsSetup.workspace_id)}>
            Open {needsSetup.name}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="snapshot-import">
      <label className="field">
        <span>Snapshot file</span>
        <input
          type="file"
          accept=".zip,application/zip"
          aria-label="Snapshot file"
          onChange={(e) => void pickFile(e.target.files?.[0] ?? null)}
        />
      </label>

      {busy === "planning" ? (
        <div className="snapshot-loading">
          <Spinner size={16} /> Reading the snapshot…
        </div>
      ) : null}

      {error ? (
        <p className="snapshot-import-error" role="alert">
          <AlertTriangle size={14} /> {error}
        </p>
      ) : null}

      {plan ? (
        <>
          <p className="snapshot-lede">{planSummary(plan)}</p>

          {plan.runs_code ? (
            <section className="snapshot-section snapshot-section--warn">
              <h3>
                <ShieldAlert size={14} /> This will install and run code
              </h3>
              <p className="snapshot-hint">
                These plugins are cloned from the URLs below and run in-process as the new
                agent, with your privileges. Import only from a source you trust.
              </p>
              <ul className="snapshot-list">
                {plan.plugins.map((p) => (
                  <li key={p.id} className="snapshot-row">
                    <code>
                      {p.url}
                      {p.ref ? `@${p.ref.slice(0, 7)}` : ""}
                    </code>
                    {p.recognized ? null : <span className="snapshot-kinds">unfamiliar source</span>}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {plan.capabilities.length ? (
            <section className="snapshot-section">
              <h3>
                <Package size={14} /> This snapshot's config grants
              </h3>
              <ul className="snapshot-list">
                {plan.capabilities.map((c) => (
                  <li key={c.key} className="snapshot-row">
                    <code>{c.key}</code>
                    <span className="snapshot-kinds">{c.grants}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <label className="field">
            <span>Name</span>
            <Input
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              placeholder="my-agent"
              aria-invalid={!nameOk}
            />
            {!nameOk ? <span className="snapshot-hint">Letters, numbers, dashes and underscores.</span> : null}
          </label>

          {needed.length ? (
            <section className="snapshot-section">
              <h3>
                <KeyRound size={14} /> Credentials this agent needs
              </h3>
              <p className="snapshot-hint">
                None travel in a snapshot. Leave any blank to fill in later — the agent is
                created either way, just not yet working.
              </p>
              {needed.map((s) => (
                <label className="field" key={s.name}>
                  <span>{s.name}</span>
                  <SecretInput
                    value={secrets[s.name] ?? ""}
                    onChange={(e) => {
                      // Read the value NOW: the functional updater runs after dispatch,
                      // when React has already nulled e.currentTarget — reading it there
                      // threw on the first keystroke and unmounted the panel.
                      const value = e.currentTarget.value;
                      setSecrets((v) => ({ ...v, [s.name]: value }));
                    }}
                    placeholder="paste the value"
                  />
                </label>
              ))}
            </section>
          ) : null}

          {plan.notes.map((n) => (
            <p className="snapshot-hint" key={n}>
              {n}
            </p>
          ))}

          <div className="snapshot-import-actions">
            <Button onClick={() => void doImport()} disabled={!nameOk || busy === "importing"}>
              {busy === "importing" ? <Spinner size={14} /> : <Upload size={14} />} {importButtonLabel(plan)}
            </Button>
          </div>
        </>
      ) : null}
    </div>
  );
}
