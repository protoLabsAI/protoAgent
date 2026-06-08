import { QueryErrorResetBoundary, useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";

import { PanelHeader } from "./PanelHeader";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";
import { api } from "../lib/api";

// Agent → MCP: external Model Context Protocol servers whose tools are wired into
// the agent (namespaced <server>__<tool>). Add/remove here hot-reloads — the new
// server's tools wire in immediately, no restart.

type Transport = "stdio" | "http" | "sse";

function AddServerForm({ onDone }: { onDone: (msg: string) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<Transport>("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");

  const reset = () => { setName(""); setCommand(""); setArgs(""); setUrl(""); setTransport("stdio"); };
  const add = useMutation({
    mutationFn: () =>
      api.addMcpServer(
        transport === "stdio"
          ? { name, transport, command, args }
          : { name, transport, url },
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      reset();
      setOpen(false);
      onDone(`Connected ${res.name} — its tools are live.`);
    },
    onError: (err: unknown) => onDone(`Couldn't add server: ${err instanceof Error ? err.message : String(err)}`),
  });

  const valid = name.trim() && (transport === "stdio" ? command.trim() : url.trim());

  if (!open) {
    return (
      <button type="button" className="ghost-button" onClick={() => setOpen(true)}>
        <Plus size={14} /> Add server
      </button>
    );
  }
  return (
    <form className="mcp-add-form" onSubmit={(e) => { e.preventDefault(); if (valid) add.mutate(); }}>
      <div className="mcp-add-row">
        <input className="playbook-search" placeholder="name (e.g. echo)" value={name} onChange={(e) => setName(e.target.value)} />
        <select className="playbook-search" value={transport} onChange={(e) => setTransport(e.target.value as Transport)}>
          <option value="stdio">stdio</option>
          <option value="http">http</option>
          <option value="sse">sse</option>
        </select>
      </div>
      {transport === "stdio" ? (
        <div className="mcp-add-row">
          <input className="playbook-search" placeholder="command (e.g. python)" value={command} onChange={(e) => setCommand(e.target.value)} />
          <input className="playbook-search" placeholder="args (space-separated)" value={args} onChange={(e) => setArgs(e.target.value)} />
        </div>
      ) : (
        <input className="playbook-search" placeholder="url (https://…)" value={url} onChange={(e) => setUrl(e.target.value)} />
      )}
      <div className="mcp-add-actions">
        <button type="submit" className="ghost-button" disabled={!valid || add.isPending}>
          {add.isPending ? <Loader2 size={14} className="spin" /> : "Connect"}
        </button>
        <button type="button" className="ghost-button" onClick={() => { reset(); setOpen(false); }}>Cancel</button>
      </div>
    </form>
  );
}

function McpBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const qc = useQueryClient();
  const [hint, setHint] = useState<string | null>(null);
  const servers = runtime.mcp?.servers ?? [];
  const total = runtime.mcp?.tool_count ?? 0;

  const remove = useMutation({
    mutationFn: (n: string) => api.removeMcpServer(n),
    onSuccess: (_res, n) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      setHint(`Removed ${n}.`);
    },
    onError: (err: unknown, n) => setHint(`Couldn't remove ${n}: ${err instanceof Error ? err.message : String(err)}`),
  });
  const removingName = remove.isPending ? remove.variables : undefined;

  return (
    <>
      <PanelHeader
        title="MCP servers"
        kicker={`${servers.length} server${servers.length === 1 ? "" : "s"} · ${total} tool${total === 1 ? "" : "s"}`}
      />
      <div className="stage-body">
        {hint ? <p className="plugin-hint">{hint}</p> : null}
        <AddServerForm onDone={setHint} />
        <div className="table-list">
          {servers.length ? (
            servers.map((server) => (
              <div className="table-row" key={server.name}>
                <span>{server.name} · {server.transport}</span>
                <div className="plugin-row-actions">
                  <StatusPill label={`${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`} tone="success" />
                  <button
                    type="button"
                    className="ghost-button"
                    disabled={removingName === server.name}
                    onClick={() => { setHint(null); remove.mutate(server.name); }}
                    title={`Remove ${server.name}`}
                  >
                    {removingName === server.name ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />}
                  </button>
                </div>
              </div>
            ))
          ) : (
            <div className="table-row">
              <span>no MCP servers configured — add one above</span>
              <StatusPill label={runtime.mcp?.enabled ? "enabled" : "off"} tone="muted" />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export function McpPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="MCP" />}>
            <Suspense fallback={<PanelSkeleton label="Loading MCP…" />}>
              <McpBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
