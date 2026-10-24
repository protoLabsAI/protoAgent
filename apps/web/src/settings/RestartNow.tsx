import { Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, currentSlug } from "../lib/api";
import type { RestartReadiness } from "../lib/api/setup";

// A plugin request and a banner can coexist; only one may control the process.
let restarting = false;
const pause = () => new Promise<void>((resolve) => setTimeout(resolve, 1000));

export function RestartNow({ labels, onRestarted, requestId = 0 }: {
  labels: string[]; onRestarted?: () => void; requestId?: number;
}) {
  const client = useQueryClient();
  const [readiness, setReadiness] = useState<RestartReadiness>();
  const [confirm, setConfirm] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");
  const cancelled = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; cancelled.current = true; };
  }, []);
  useEffect(() => {
    let live = true;
    void api.restartReadiness().then((value) => {
      if (!live) return;
      setReadiness(value);
      if (requestId && value.supervised) setConfirm(true);
    }).catch(() => { if (live) setError("Could not check whether the server can restart. Try again."); });
    return () => { live = false; };
  }, [requestId]);

  async function ask() {
    setError("");
    try {
      const value = await api.restartReadiness();
      setReadiness(value);
      if (value.supervised) setConfirm(true);
    } catch { setError("Could not check whether the server can restart. Try again."); }
  }

  async function run() {
    setConfirm(false);
    if (restarting) { setError("A restart is already in progress."); return; }
    restarting = true;
    cancelled.current = false;
    const member = currentSlug();
    const reason = `settings: ${labels.join(", ") || "plugin settings"}`;
    try {
      setError("");
      setPhase("Waiting for idle…");
      let before: RestartReadiness;
      for (;;) {
        if (cancelled.current || currentSlug() !== member) return;
        before = await api.restartReadiness();
        if (cancelled.current || currentSlug() !== member) return;
        setReadiness(before);
        if (!before.supervised) throw new Error(before.how_to);
        if (before.idle) break;
        await pause();
      }
      setPhase("Restarting…");
      if (member !== "host") {
        const stopped = await api.stopForSettings(member, reason);
        if (!stopped.ok) throw new Error("The agent could not stop. Try again.");
        const started = await api.startAgent(member);
        if (!started.ok) throw new Error("The agent could not start. Start it from Fleet.");
      } else {
        await api.restartForSettings(reason);
      }
      setPhase("Waiting for the server to reconnect…");
      // A successful read from the OLD process is not proof of a restart: wait for
      // a fresh process identity, including when shutdown is too fast to observe.
      for (let attempt = 0; attempt < 120; attempt++) {
        await pause();
        if (!mounted.current || currentSlug() !== member) return;
        try {
          const after = await api.restartReadiness();
          if (!after.boot_id || after.boot_id === before.boot_id) continue;
        } catch { continue; }
        await client.invalidateQueries();
        if (!mounted.current || currentSlug() !== member) return;
        window.dispatchEvent(new Event("protoagent:settings-restarted"));
        onRestarted?.();
        setPhase("");
        return;
      }
      throw new Error("The server has not reconnected yet. Check that it is running, then try again.");
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : "Restart failed. Try again.");
    } finally {
      restarting = false;
      if (mounted.current) setPhase("");
    }
  }

  return <>
    {error && <span role="alert">{error}</span>}
    {phase ? <span role="status">{phase} {readiness?.busy.join(", ")}
      {phase === "Waiting for idle…" && <Button size="sm" onClick={() => { cancelled.current = true; }}>Cancel</Button>}
    </span> : readiness && !readiness.supervised ? <span>{readiness.how_to}</span> : requestId ? null : <>
      {readiness?.busy.length ? <span>{readiness.busy.join(", ")}. </span> : null}
      <Button size="sm" disabled={!readiness && !error} onClick={() => void ask()}>
        {readiness && !readiness.idle ? "Restart when idle" : "Restart now"}
      </Button>
    </>}
    <ConfirmDialog open={confirm} title="Restart this agent?"
      confirmLabel={readiness?.idle ? "Restart now" : "Restart when idle"}
      onConfirm={() => void run()} onClose={() => setConfirm(false)}>
      <p>{readiness?.busy.length ? `${readiness.busy.join(", ")}. ` : ""}
        Apply {labels.join(", ") || "saved settings"} by restarting once the agent is idle.
        Keep this console open; it will reconnect automatically.</p>
    </ConfirmDialog>
  </>;
}
