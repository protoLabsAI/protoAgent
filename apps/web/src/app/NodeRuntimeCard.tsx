import { Banner, Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Download } from "lucide-react";

import { nodeRuntimeQuery, runtimeStatusQuery } from "../lib/queries";
import { nodeRuntimeView } from "./nodeRuntime";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";

/** Managed Node runtime (ADR 0085). The npx-based ACP coding agents (Claude Code, Codex)
 *  and npx-based MCP servers need node/npx on PATH; a machine with no Node has none. This
 *  surfaces one-click provisioning right where the gap bites (the MCP panel). It renders
 *  nothing when a Node is already usable — only a call to action when there isn't one. */
export function NodeRuntimeCard() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data } = useQuery(nodeRuntimeQuery());
  const view = nodeRuntimeView(data);

  const install = useMutation({
    mutationFn: () => api.installNodeRuntime(),
    onError: (err: unknown) => toast({ tone: "error", title: "Couldn't start install", message: errMsg(err) }),
    // The POST returns 202 immediately; refetch so the query flips to "running" and polls.
    onSettled: () => qc.invalidateQueries({ queryKey: nodeRuntimeQuery().queryKey }),
  });

  // When an install lands, tell the operator and refresh the MCP/runtime view so the
  // npx-based servers they couldn't add before can be retried.
  const prev = useRef<string | undefined>(undefined);
  useEffect(() => {
    const state = data?.install.state;
    if (prev.current === "running" && state === "done") {
      toast({
        tone: "success",
        title: "Node runtime installed",
        message: `${data?.node.version ?? ""} — npx-based servers and coding agents can now launch.`.trim(),
      });
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
    } else if (prev.current === "running" && state === "error") {
      toast({ tone: "error", title: "Install failed", message: data?.install.error ?? "unknown error" });
    }
    prev.current = state;
  }, [data?.install.state, data?.node.version, data?.install.error, qc, toast]);

  if (view.kind === "hidden") return null;

  if (view.kind === "unsupported") {
    return (
      <Banner tone="warning" title="No managed Node build for this platform" className="shell-warning-banner">
        Install Node yourself to use <code>npx</code>-based MCP servers and the Claude/Codex coding agents.
      </Banner>
    );
  }

  const installing = view.installing || install.isPending;
  return (
    <Banner
      tone="warning"
      title={installing ? "Installing Node runtime…" : "No Node runtime detected"}
      className="shell-warning-banner"
      action={
        <Button size="sm" variant="primary" loading={installing} onClick={() => install.mutate()}>
          <Download size={14} /> {view.error ? "Retry install" : "Install runtime"}
        </Button>
      }
    >
      {installing ? (
        <>Downloading and verifying Node{view.pct ? ` — ${view.pct}%` : ""}. This runs once.</>
      ) : view.error ? (
        <>Provisioning failed: {view.error}. The <code>npx</code>-based servers and coding agents can't launch yet.</>
      ) : (
        <>
          <code>npx</code>-based MCP servers and the Claude/Codex coding agents can't launch. Provision a
          managed Node runtime (a one-time, hash-verified download) to enable them.
        </>
      )}
    </Banner>
  );
}
