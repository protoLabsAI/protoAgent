import { DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ArrowDownFromLine, ArrowUpToLine, Boxes, Library, Loader2, Plus, Share2, Trash2 } from "lucide-react";

import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { runtimeStatusQuery } from "../lib/queries";
import { StagePanel } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";
import { McpCatalogDialog } from "./McpCatalogDialog";
import { QuickSetting } from "../settings/QuickSetting";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";

type McpServer = { name: string; transport: string; tool_count: number; tier?: "commons" | "private" | "managed" | null };

// Agent → MCP: external Model Context Protocol servers whose tools are wired into
// the agent (namespaced <server>__<tool>). Add/remove here hot-reloads — the new
// server's tools wire in immediately, no restart.

type Transport = "stdio" | "http" | "sse";

function AddServerForm() {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"form" | "json">("form");
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<Transport>("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [json, setJson] = useState("");

  const reset = () => {
    setName(""); setCommand(""); setArgs(""); setUrl(""); setTransport("stdio"); setJson("");
  };
  const onSuccess = (msg: string) => {
    qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
    reset();
    setOpen(false);
    toast({ tone: "success", title: "MCP server", message: msg });
  };
  const add = useMutation({
    mutationFn: () =>
      api.addMcpServer(transport === "stdio" ? { name, transport, command, args } : { name, transport, url }),
    onSuccess: (res) => onSuccess(`Connected ${res.name} — its tools are live.`),
    onError: (err: unknown) => toast({ tone: "error", title: "Couldn't add server", message: errMsg(err) }),
  });
  const importJson = useMutation({
    mutationFn: () => api.importMcpServers(json),
    onSuccess: (res) => onSuccess(`Imported ${res.added.length} server${res.added.length === 1 ? "" : "s"}: ${res.added.join(", ")}.`),
    onError: (err: unknown) => toast({ tone: "error", title: "Import failed", message: errMsg(err) }),
  });

  const formValid = name.trim() && (transport === "stdio" ? command.trim() : url.trim());
  const busy = add.isPending || importJson.isPending;

  if (!open) {
    return (
      <Button type="button" variant="ghost" onClick={() => setOpen(true)}>
        <Plus size={14} /> Add server
      </Button>
    );
  }
  return (
    <form
      className="mcp-add-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (mode === "json") { if (json.trim()) importJson.mutate(); }
        else if (formValid) add.mutate();
      }}
    >
      <div className="mcp-add-modes">
        <Tabs
          variant="segmented"
          ariaLabel="Add-server input mode"
          active={mode}
          onSelect={(t) => setMode(t as "form" | "json")}
          items={[
            { id: "form", label: "Form" },
            { id: "json", label: "Paste JSON" },
          ]}
        />
      </div>

      {mode === "json" ? (
        <Textarea
          className="playbook-search mcp-json"
          rows={8}
          placeholder={'Paste a server config, e.g.\n{\n  "mcpServers": {\n    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] }\n  }\n}'}
          value={json}
          onChange={(e) => setJson(e.target.value)}
        />
      ) : (
        <>
          <div className="mcp-add-row">
            <Input className="playbook-search" placeholder="name (e.g. echo)" value={name} onChange={(e) => setName(e.target.value)} />
            <DropdownSelect
              className="playbook-search"
              value={transport}
              onValueChange={(v) => setTransport(v as Transport)}
              options={[
                { value: "stdio", label: "stdio" },
                { value: "http", label: "http" },
                { value: "sse", label: "sse" },
              ]}
            />
          </div>
          {transport === "stdio" ? (
            <div className="mcp-add-row">
              <Input className="playbook-search" placeholder="command (e.g. python)" value={command} onChange={(e) => setCommand(e.target.value)} />
              <Input className="playbook-search" placeholder="args (space-separated)" value={args} onChange={(e) => setArgs(e.target.value)} />
            </div>
          ) : (
            <Input className="playbook-search" placeholder="url (https://…)" value={url} onChange={(e) => setUrl(e.target.value)} />
          )}
        </>
      )}

      <div className="mcp-add-actions">
        <Button type="submit" variant="ghost" disabled={busy || (mode === "json" ? !json.trim() : !formValid)}>
          {busy ? <Loader2 size={14} className="spin" /> : mode === "json" ? "Import" : "Connect"}
        </Button>
        <Button type="button" variant="ghost" onClick={() => { reset(); setOpen(false); }}>Cancel</Button>
      </div>
    </form>
  );
}

function McpBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const qc = useQueryClient();
  const toast = useToast();
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [forgetPending, setForgetPending] = useState<McpServer | null>(null);
  const servers = (runtime.mcp?.servers ?? []) as McpServer[];
  const total = runtime.mcp?.tool_count ?? 0;
  // The agent participates in the box commons (mcp.scope: layered) when any server
  // carries a tier — then we surface tier badges + share/unshare (ADR 0041).
  const layered = servers.some((s) => s.tier);

  const remove = useMutation({
    mutationFn: (n: string) => api.removeMcpServer(n),
    onSuccess: (_res, n) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      toast({ tone: "success", title: "Server removed", message: `${n} is no longer wired in.` });
    },
    onError: (err: unknown, n) => toast({ tone: "error", title: "Couldn't remove server", message: `${n}: ${errMsg(err)}` }),
  });
  const removingName = remove.isPending ? remove.variables : undefined;

  const promote = useMutation({
    mutationFn: (n: string) => api.promoteMcpServer(n),
    onSuccess: (_res, n) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      toast({ tone: "success", title: "Shared to the box commons", message: `Every layered agent on this box now runs ${n}.` });
    },
    onError: (err: unknown, n) => toast({ tone: "error", title: "Couldn't share server", message: `${n}: ${errMsg(err)}` }),
  });
  const forget = useMutation({
    mutationFn: (n: string) => api.forgetMcpServer(n),
    onSuccess: (_res, n) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      toast({ tone: "success", title: "Unshared", message: `${n} is private to this agent again.` });
    },
    onError: (err: unknown, n) => toast({ tone: "error", title: "Couldn't unshare server", message: `${n}: ${errMsg(err)}` }),
  });
  const busyName = promote.isPending ? promote.variables : forget.isPending ? forget.variables : undefined;

  return (
    <>
      <PanelHeader
        title="MCP servers"
        kicker={`${servers.length} server${servers.length === 1 ? "" : "s"} · ${total} tool${total === 1 ? "" : "s"}`}
      />
      <div className="stage-body">
        <div className="mcp-browse-row">
          <Button type="button" variant="ghost" onClick={() => setCatalogOpen(true)} title="Add a common MCP server from a curated list">
            <Boxes size={14} /> Browse common servers
          </Button>
          <QuickSetting keys={["mcp.scope"]} title="MCP server sharing" label="MCP server sharing" icon={<Share2 size={16} />} />
        </div>
        <AddServerForm />
        <McpCatalogDialog open={catalogOpen} onClose={() => setCatalogOpen(false)} />
        <div className="table-list">
          {servers.length ? (
            servers.map((server) => (
              <div className="table-row" key={server.name}>
                <span className="mcp-server-name">
                  {layered && server.tier === "commons" ? (
                    <span title="Shared commons — runs on every layered agent on this box">
                      <Badge status="neutral"><Library size={12} /> commons</Badge>
                    </span>
                  ) : layered && server.tier === "private" ? (
                    <span title="Private to this agent — share to run it on every layered agent on this box">
                      <Badge status="neutral">private</Badge>
                    </span>
                  ) : null}
                  {server.name} · {server.transport}
                </span>
                <div className="plugin-row-actions">
                  <StatusPill label={`${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`} tone="success" />
                  {layered && server.tier === "private" ? (
                    <Button type="button" variant="ghost" disabled={busyName === server.name}
                      onClick={() => promote.mutate(server.name)}
                      title={`Share ${server.name} to the box commons`} aria-label={`share ${server.name}`}
                    >
                      <ArrowUpToLine size={14} className={busyName === server.name ? "spin" : ""} />
                    </Button>
                  ) : null}
                  {layered && server.tier === "commons" ? (
                    <Button type="button" variant="ghost" disabled={busyName === server.name}
                      onClick={() => setForgetPending(server)}
                      title={`Unshare ${server.name} from the box commons`} aria-label={`unshare ${server.name}`}
                    >
                      <ArrowDownFromLine size={14} />
                    </Button>
                  ) : null}
                  <Button type="button"
                    variant="ghost"
                    disabled={removingName === server.name}
                    onClick={() => remove.mutate(server.name)}
                    title={`Remove ${server.name}`}
                  >
                    {removingName === server.name ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />}
                  </Button>
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

      <ConfirmDialog
        open={forgetPending !== null}
        title="Unshare from the box commons?"
        confirmLabel="Unshare"
        destructive
        onConfirm={() => { if (forgetPending) forget.mutate(forgetPending.name); setForgetPending(null); }}
        onClose={() => setForgetPending(null)}
      >
        {forgetPending
          ? `"${forgetPending.name}" will be removed from the box commons and become private to this agent — no other agent on this box will run it.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}

export function McpPanel() {
  return (
    <StagePanel label="MCP">
      <McpBody />
    </StagePanel>
  );
}
