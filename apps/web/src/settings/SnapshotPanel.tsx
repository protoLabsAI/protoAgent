import { Spinner } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { AlertTriangle, Download, FolderSymlink, KeyRound, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { api } from "../lib/api";
import { SettingsSubPanel } from "./SettingsSubPanel";
import { splitFindings } from "./snapshotReview";
import "./snapshot.css";

import type { SnapshotReview } from "../lib/types";

/**
 * Settings ▸ Agent ▸ Snapshot — export this agent as a portable, secret-free zip (ADR 0091).
 *
 * Sits last in the AGENT group because it exports what every section above it configures:
 * identity, model, behavior, plugins, MCP, skills. A snapshot is the agent's *definition*.
 *
 * **Review before download, not after.** The artifact is meant to leave the machine, so the
 * panel opens on the dry-run review — what travels, what the target must re-supply, what the
 * pattern sweep scrubbed — and only then offers the zip. Handing over a file nobody has read
 * is exactly the failure mode the whole feature is built to avoid.
 *
 * Findings are split by what the operator should DO about them, not by detector: a scrubbed
 * credential means "this is still live in your agent — rotate it"; a scrubbed home path means
 * "nothing to rotate, re-point it on the target". Conflating them sends someone hunting a
 * breach that never happened. The split lives in `snapshotReview.ts` — pure and unit-tested,
 * the repo's extract-then-test pattern.
 */
export function SnapshotPanel() {
  const toast = useToast();
  const [review, setReview] = useState<SnapshotReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setReview(await api.snapshotReview());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const download = async () => {
    setDownloading(true);
    try {
      const { blob, filename } = await api.exportSnapshot();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      toast({ tone: "success", title: "Snapshot downloaded", message: filename });
    } catch (e) {
      toast({
        tone: "error",
        title: "Export failed",
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setDownloading(false);
    }
  };

  const { credentials, machineLocal } = splitFindings(review?.pattern_redactions ?? {});
  const secrets = review?.required_secrets ?? [];

  return (
    <SettingsSubPanel
      label="Snapshot"
      title="Snapshot"
      kicker="export this agent's definition — secret-free, portable"
      actions={
        <>
          <Button size="sm" variant="ghost" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={14} /> Re-check
          </Button>
          <Button size="sm" onClick={() => void download()} disabled={loading || downloading || !!error}>
            {downloading ? <Spinner size={14} /> : <Download size={14} />} Download snapshot
          </Button>
        </>
      }
    >
      <p className="snapshot-lede">
        A <strong>recipe, not a backup</strong>: persona, config, plugin pins, MCP servers and skills.
        No conversation history, no credentials, no plugin code — importing yields a <em>fresh</em>{" "}
        agent, not a resumed one.
      </p>

      {loading ? (
        <div className="snapshot-loading">
          <Spinner size={16} /> Checking what would travel…
        </div>
      ) : error ? (
        <Empty title="Couldn't build the review" description={error} />
      ) : (
        <>
          <section className="snapshot-section">
            <h3>
              <KeyRound size={14} /> Credentials the target must supply
            </h3>
            {secrets.length === 0 ? (
              <p className="snapshot-none">This agent has no configured credentials.</p>
            ) : (
              <>
                <ul className="snapshot-list">
                  {secrets.map((s) => (
                    <li key={s.name} className="snapshot-row">
                      <code>{s.name}</code>
                      <StatusPill
                        tone={s.was_set ? "success" : "muted"}
                        label={s.was_set ? "set here" : "declared, unset"}
                      />
                    </li>
                  ))}
                </ul>
                <p className="snapshot-hint">
                  Names only — no values travel. <em>Set here</em> means this agent has one, so the
                  target genuinely needs it.
                </p>
              </>
            )}
          </section>

          {credentials.length > 0 ? (
            <section className="snapshot-section snapshot-section--warn">
              <h3>
                <AlertTriangle size={14} /> Credential-shaped text found and scrubbed
              </h3>
              <p className="snapshot-hint">
                Gone from the snapshot — but <strong>still in this agent</strong>. Treat it as
                exposed: rotate it, then remove it here.
              </p>
              <ul className="snapshot-list">
                {credentials.map(({ where, kinds }) => (
                  <li key={where} className="snapshot-row">
                    <code>{where || "(root)"}</code>
                    <span className="snapshot-kinds">{kinds.join(", ")}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {machineLocal.length > 0 ? (
            <section className="snapshot-section">
              <h3>
                <FolderSymlink size={14} /> Machine-local paths to re-point
              </h3>
              <p className="snapshot-hint">
                Not credentials — nothing to rotate. Scrubbed because they carry your username, and
                they wouldn't have resolved on the target anyway.
              </p>
              <ul className="snapshot-list">
                {machineLocal.map(({ where, kinds }) => (
                  <li key={where} className="snapshot-row">
                    <code>{where || "(root)"}</code>
                    <span className="snapshot-kinds">{kinds.join(", ")}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {review?.notes?.length ? (
            <section className="snapshot-section">
              <h3>Notes</h3>
              <ul className="snapshot-list">
                {review.notes.map((n) => (
                  <li key={n} className="snapshot-note">
                    {n}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <p className="snapshot-caveat">
            Scrubbing free text is a <strong>safety net, not a guarantee</strong> — it can't
            recognize a credential that reads like ordinary prose. The zip carries this same review
            as <code>REVIEW.md</code>; read it before publishing the file anywhere.
          </p>
        </>
      )}
    </SettingsSubPanel>
  );
}
