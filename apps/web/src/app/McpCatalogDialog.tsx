import { Input, SecretInput } from "@protolabsai/ui/forms";
import { Tabs } from "@protolabsai/ui/navigation";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { runtimeStatusQuery } from "../lib/queries";
import type { McpCatalogEntry } from "../lib/types";

// Quick-add for common MCP servers (Settings ▸ MCP). A curated directory
// (GET /api/mcp/catalog) of cards; picking one fills its `${input}` placeholders
// (a path, an API token) and POSTs the composed entry through the normal add path,
// so its tools wire in immediately (hot reload). Servers needing no input add in
// one click; the rest open a small configure step.

const MCP_CATALOG_KEY = ["mcp-catalog"] as const;

// Substitute ${key} placeholders in every string of the template (args, env,
// url, headers) with the operator-supplied values.
function fillTemplate(
  template: Record<string, unknown>,
  values: Record<string, string>,
): Record<string, unknown> {
  const sub = (v: unknown): unknown => {
    if (typeof v === "string") return v.replace(/\$\{(\w+)\}/g, (_m, k: string) => values[k] ?? "");
    if (Array.isArray(v)) return v.map(sub);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>).map(([k, val]) => [k, sub(val)]),
      );
    }
    return v;
  };
  return sub(template) as Record<string, unknown>;
}

export function McpCatalogDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const catalog = useQuery({
    queryKey: MCP_CATALOG_KEY,
    queryFn: () => api.mcpCatalog(),
    enabled: open,
    retry: false,
  });
  const [query, setQuery] = useState("");
  const [cat, setCat] = useState("All");
  const [selected, setSelected] = useState<McpCatalogEntry | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});

  const servers = catalog.data?.servers ?? [];
  const categories = useMemo(
    () => ["All", ...Array.from(new Set(servers.map((s) => s.category || "Other"))).sort()],
    [servers],
  );
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return servers.filter((s) => {
      if (cat !== "All" && (s.category || "Other") !== cat) return false;
      if (!q) return true;
      return `${s.name} ${s.tagline ?? ""} ${s.id}`.toLowerCase().includes(q);
    });
  }, [servers, query, cat]);

  const add = useMutation({
    mutationFn: (entry: McpCatalogEntry) => api.addMcpServer(fillTemplate(entry.template, values)),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      qc.invalidateQueries({ queryKey: MCP_CATALOG_KEY });
      toast({ tone: "success", title: "MCP server", message: `Connected ${res.name} — its tools are live.` });
      close();
    },
    onError: (e: unknown) => toast({ tone: "error", title: "Couldn't add server", message: errMsg(e) }),
  });

  function back() {
    setSelected(null);
    setValues({});
  }
  function close() {
    back();
    setQuery("");
    setCat("All");
    onClose();
  }
  function pick(entry: McpCatalogEntry) {
    if (entry.inputs?.length) {
      setSelected(entry);
      setValues(Object.fromEntries(entry.inputs.map((i) => [i.key, ""])));
    } else {
      add.mutate(entry);
    }
  }

  if (!open) return null;

  const missing = selected?.inputs?.some((i) => i.required && !values[i.key]?.trim()) ?? false;

  return (
    <Dialog
      open
      onClose={close}
      title="Add a common MCP server"
      width="min(720px, 95vw)"
      className={`mcp-catalog-dialog${selected ? "" : " mcp-catalog-dialog--browse"}`}
    >
      {selected ? (
        <div className="mcp-catalog-configure">
          <button type="button" className="mcp-catalog-back" onClick={back}>
            <ArrowLeft size={14} /> All servers
          </button>
          <div className="mcp-catalog-config-head">
            <strong>{selected.name}</strong>
            {selected.requires ? <span className="mcp-catalog-chip">needs {selected.requires}</span> : null}
          </div>
          <p className="mcp-catalog-tagline">{selected.tagline}</p>
          {selected.inputs?.map((inp) => (
            <label key={inp.key} className="mcp-catalog-field">
              <span>
                {inp.label}
                {inp.required ? " *" : ""}
              </span>
              {inp.secret ? (
                <SecretInput
                  placeholder={inp.placeholder}
                  value={values[inp.key] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [inp.key]: e.target.value }))}
                  aria-label={inp.label}
                />
              ) : (
                <Input
                  type="text"
                  placeholder={inp.placeholder}
                  value={values[inp.key] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [inp.key]: e.target.value }))}
                  aria-label={inp.label}
                />
              )}
            </label>
          ))}
          <div className="mcp-add-actions">
            <Button
              type="button"
              variant="primary"
              loading={add.isPending}
              disabled={missing}
              onClick={() => add.mutate(selected)}
            >
              {add.isPending ? null : <Plus size={14} />} Add server
            </Button>
            <Button type="button" variant="ghost" onClick={back}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="mcp-catalog-controls">
            <Input
              className="mcp-catalog-search"
              icon={<Search size={14} />}
              type="search"
              placeholder="Search servers"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="search MCP servers"
            />
            <Tabs
              variant="segmented"
              responsive
              ariaLabel="filter servers by category"
              items={categories.map((c) => ({ id: c, label: c }))}
              active={cat}
              onSelect={setCat}
            />
          </div>
          {catalog.isError ? (
            <p className="plugin-hint">Couldn't load the server directory.</p>
          ) : !shown.length ? (
            <p className="plugin-hint">{catalog.isLoading ? "Loading…" : "No servers match."}</p>
          ) : (
            <div className="mcp-catalog-grid">
              {shown.map((s) => (
                <div className="mcp-catalog-card" key={s.id}>
                  <div className="mcp-catalog-card-head">
                    <strong>{s.name}</strong>
                    {s.category ? <span className="mcp-catalog-chip">{s.category}</span> : null}
                  </div>
                  <p className="mcp-catalog-tagline">{s.tagline}</p>
                  <div className="mcp-catalog-foot">
                    {s.docs ? (
                      <a className="mcp-catalog-docs" href={s.docs} target="_blank" rel="noreferrer">
                        <ExternalLink size={12} /> docs
                      </a>
                    ) : (
                      <span />
                    )}
                    {s.installed ? (
                      <Badge status="success">added</Badge>
                    ) : (
                      <Button type="button" variant="ghost" onClick={() => pick(s)} disabled={add.isPending}>
                        <Plus size={14} /> Add
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Dialog>
  );
}
