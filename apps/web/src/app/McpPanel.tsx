import { DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";

import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { runtimeStatusQuery } from "../lib/queries";
import { StagePanel } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";

// Agent → MCP: external Model Context Protocol servers whose tools are wired into
// the agent (namespaced <server>__<tool>). Add/remove here hot-reloads — the new
// server's tools wire in immediately, no restart.

type Transport = "stdio" | "http" | "sse";

function AddServerForm({ onDone }: { onDone: (msg: string) => void }) {
  const qc = useQueryClient();
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
    onDone(msg);
  };
  const add = useMutation({
    mutationFn: () =>
      api.addMcpServer(transport === "stdio" ? { name, transport, command, args } : { name, transport, url }),
    onSuccess: (res) => onSuccess(`Connected ${res.name} — its tools are live.`),
    onError: (err: unknown) => onDone(`Couldn't add server: ${errMsg(err)}`),
  });
  const importJson = useMutation({
    mutationFn: () => api.importMcpServers(json),
    onSuccess: (res) => onSuccess(`Imported ${res.added.length} server${res.added.length === 1 ? "" : "s"}: ${res.added.join(", ")}.`),
    onError: (err: unknown) => onDone(`Import failed: ${errMsg(err)}`),
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
  const [hint, setHint] = useState<string | null>(null);
  const servers = runtime.mcp?.servers ?? [];
  const total = runtime.mcp?.tool_count ?? 0;

  const remove = useMutation({
    mutationFn: (n: string) => api.removeMcpServer(n),
    onSuccess: (_res, n) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      setHint(`Removed ${n}.`);
    },
    onError: (err: unknown, n) => setHint(`Couldn't remove ${n}: ${errMsg(err)}`),
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
                  <Button type="button"
                    variant="ghost"
                    disabled={removingName === server.name}
                    onClick={() => { setHint(null); remove.mutate(server.name); }}
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
