import "./tools.css";

import { Input, Switch } from "@protolabsai/ui/forms";
import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { FolderTree, TerminalSquare } from "lucide-react";

import { Accordion, AccordionItem, PanelHeader } from "@protolabsai/ui/navigation";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, toolsQuery } from "../lib/queries";
import type { FsProject } from "../lib/types";
import { QuickSetting } from "../settings/QuickSetting";
import { useUI } from "../state/uiStore";
import { StagePanel } from "./ErrorBoundary";

// Runtime → Tools: the live tool inventory the lead agent + subagents can call.
// Core tools group by subsystem; plugin tools group by the PLUGIN that brought them
// (backend stamps the category); MCP tools by server. Order: core subsystems first
// (CORE_ORDER), then plugin groups (alpha), then MCP — so the baseline reads top-down
// and everything an extension adds sits below it. Searchable; a search expands matches.
// Every row carries an on/off switch editing the tools.disabled denylist — a toggled-off
// tool stays listed (the backend catalogs dropped tools too) so it can be re-enabled.

// Core subsystem order. Plugin/MCP group names are dynamic, so they're NOT listed here —
// they sort after core by source rank (below).
const CORE_ORDER = [
  "General", "Filesystem", "Skills", "Web & research", "Memory", "Scheduler",
  "Inbox", "Tasks", "Goals", "Delegation", "Workflows", "Discovery",
];
// core baseline first, then plugin-contributed, then MCP.
const SOURCE_RANK: Record<string, number> = { core: 0, plugin: 1, mcp: 2 };

function ToolsBody() {
  const { data } = useSuspenseQuery(toolsQuery());
  const [q, setQ] = useState("");
  const queryClient = useQueryClient();
  const toast = useToast();

  // Deep-link from a chat tool card's "Manage" (#1803): the target tool name arrives as a
  // one-shot in the UI store. Prefill the search with it — that both filters to the tool and
  // auto-expands its group (the AccordionItem's defaultOpen keys off a non-empty query) — then
  // highlight the row and consume the one-shot so a later manual search isn't hijacked.
  const toolsTarget = useUI((s) => s.toolsTarget);
  const setToolsTarget = useUI((s) => s.setToolsTarget);
  const [highlight, setHighlight] = useState<string | null>(null);
  useEffect(() => {
    if (!toolsTarget) return;
    setQ(toolsTarget);
    setHighlight(toolsTarget);
    setToolsTarget(null);
  }, [toolsTarget, setToolsTarget]);
  // The highlight is a momentary "here it is" cue, not a persistent selection — drop it after
  // it's drawn the eye (the prefilled search stays, so the row remains in view).
  useEffect(() => {
    if (!highlight) return;
    const t = setTimeout(() => setHighlight(null), 2600);
    return () => clearTimeout(t);
  }, [highlight]);
  // Scroll the just-highlighted row into view once it mounts (the search remounts the sections).
  const scrollToTarget = (el: HTMLDivElement | null) => {
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  // Per-row on/off = editing the tools.disabled denylist — the same config the YAML /
  // central-settings route writes, enforced over the FULL assembled set (#1612), so a
  // switch here and `tools.disabled: [run_command]` are literally the same thing.
  const toggle = useMutation({
    mutationFn: ({ next }: { name: string; enabled: boolean; next: string[] }) =>
      api.saveSettings({ "tools.disabled": next }, "agent"),
    // Optimistic flip so the switch tracks the click; the settled refetch below is
    // authoritative (the save hot-rebuilds the graph and its bound/disabled catalog).
    onMutate: async ({ name, enabled, next }) => {
      const key = toolsQuery().queryKey;
      await queryClient.cancelQueries({ queryKey: key });
      const prev = queryClient.getQueryData(key);
      queryClient.setQueryData(key, (old: typeof data | undefined) =>
        old && {
          ...old,
          tools: old.tools.map((t) => (t.name === name ? { ...t, enabled } : t)),
          count: old.count + (enabled ? 1 : -1),
          disabled: next,
        });
      return { key, prev };
    },
    // On failure, roll the optimistic flip back to the snapshot NOW — the settled
    // refetch is authoritative but can itself fail (often for the same reason the
    // save did), which would strand the never-persisted state in the cache; onToggle
    // computes the next payload from that cache, so a stale flip would compound.
    onSuccess: (r, _vars, ctx) => {
      if (r.ok) return;
      if (ctx?.prev !== undefined) queryClient.setQueryData(ctx.key, ctx.prev);
      toast({ tone: "error", title: "Toggle failed", message: r.messages.join(" · ") });
    },
    onError: (e, _vars, ctx) => {
      if (ctx?.prev !== undefined) queryClient.setQueryData(ctx.key, ctx.prev);
      toast({ tone: "error", title: "Toggle failed", message: errMsg(e) });
    },
    onSettled: (_r, _e, _vars, ctx) => {
      void queryClient.invalidateQueries({ queryKey: ctx?.key ?? queryKeys.tools });
      // tools.disabled's current value also renders in the settings schema (central home).
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
  });

  const onToggle = (name: string, enabled: boolean) => {
    // Edit the RAW denylist the backend echoes (freshest cache copy, so rapid toggles
    // compound instead of clobbering) — never recompute it from the visible rows, or a
    // stale entry (a disabled tool from a since-removed plugin) would be silently lost.
    const cur = queryClient.getQueryData(toolsQuery().queryKey)?.disabled ?? data.disabled;
    const next = cur.filter((n) => n !== name);
    if (!enabled) next.push(name);
    toggle.mutate({ name, enabled, next });
  };

  const query = q.trim().toLowerCase();
  const tools = query
    ? data.tools.filter((t) =>
        `${t.name} ${t.description} ${t.source} ${t.category ?? ""}`.toLowerCase().includes(query))
    : data.tools;

  // Group by category. Each group is homogeneous in source (a core subsystem, one
  // plugin's tools, or MCP), so the group's source = its first tool's.
  const groups = new Map<string, typeof tools>();
  for (const t of tools) {
    const cat = t.category || "General";
    (groups.get(cat) ?? groups.set(cat, []).get(cat)!).push(t);
  }
  const groupSource = (cat: string) => groups.get(cat)![0]?.source ?? "core";
  // Order by source rank (core → plugin → MCP); within core by CORE_ORDER (then alpha
  // for any unlisted core group), within plugin/MCP alphabetically.
  const ordered = [...groups.keys()].sort((a, b) => {
    const sa = SOURCE_RANK[groupSource(a)] ?? 1, sb = SOURCE_RANK[groupSource(b)] ?? 1;
    if (sa !== sb) return sa - sb;
    if (groupSource(a) === "core") {
      const ia = CORE_ORDER.indexOf(a), ib = CORE_ORDER.indexOf(b);
      const ra = ia === -1 ? 99 : ia, rb = ib === -1 ? 99 : ib;
      if (ra !== rb) return ra - rb;
    }
    return a.localeCompare(b);
  });

  const off = data.tools.length - data.count;
  return (
    <>
      <PanelHeader
        title="Tools"
        kicker={`${data.count} wired tool${data.count === 1 ? "" : "s"}${off ? ` · ${off} off` : ""} · ${groups.size} group${groups.size === 1 ? "" : "s"}`}
      />
      <div className="stage-body">
        {/* The shell/fs execution policy + Work folders used to float here, above the search,
            governing tools most of the list isn't. They now live INSIDE the Filesystem group
            (below), contextual to the tools they gate — the search is the first control. */}
        <Input
          className="playbook-search"
          type="search"
          placeholder="Search tools…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {ordered.length ? (
          <Accordion className="tools-groups">
            {ordered.map((cat, i) => {
              const items = groups.get(cat)!;
              const offCount = items.filter((t) => !t.enabled).length;
              return (
                <AccordionItem
                  // Re-key on the query so a search remounts the sections with the
                  // "expand every match" defaultOpen (defaultOpen only applies on mount).
                  key={`${cat}::${query}`}
                  defaultOpen={Boolean(query) || i === 0}
                  title={
                    <span className="tools-group-head">
                      {cat}
                      {/* A group that carries contextual settings flags it — the terminal glyph
                          the shell/fs config used to wear, now on the group it belongs to. */}
                      {cat === "Filesystem" ? (
                        <TerminalSquare size={13} className="tools-group-cog" aria-label="has settings" />
                      ) : null}
                      <Badge status="neutral">{items.length}</Badge>
                      {/* The group is homogeneous in source, so the source belongs on the
                          GROUP, not repeated on every row. Core is the baseline (no chip);
                          plugin/MCP groups get a chip so what an extension added stands out. */}
                      {groupSource(cat) !== "core" ? (
                        <Badge status="neutral">{groupSource(cat)}</Badge>
                      ) : null}
                      {offCount ? <Badge status="warning">{offCount} off</Badge> : null}
                    </span>
                  }
                >
                  <div className="tools-list">
                    {/* Contextual group settings (ADR 0048): the shell/fs EXECUTION policy (enable ·
                        run · approval · /bypass) and the Work-folders fence live WITH the tools they
                        gate — chips on the group that open a dialog (like a plugin's Configure), not
                        global chrome above the search. Same /api/settings + fs-projects save paths. */}
                    {cat === "Filesystem" ? (
                      <div className="tools-group-actions">
                        <QuickSetting
                          keys={[
                            "filesystem.enabled",
                            "filesystem.allow_run",
                            "filesystem.run_requires_approval",
                            "filesystem.bypass_allowed",
                          ]}
                          title="Shell & filesystem tools"
                          label="Shell & filesystem tools"
                          icon={<TerminalSquare size={15} />}
                        />
                        <WorkFoldersButton />
                      </div>
                    ) : null}
                    {items.map((t) => (
                      <div
                        className={`tools-row${t.enabled ? "" : " tools-row--off"}${t.name === highlight ? " tools-row--target" : ""}`}
                        key={t.name}
                        ref={t.name === highlight ? scrollToTarget : undefined}
                      >
                        <div className="tools-row-main">
                          <code className="tools-name">{t.name}</code>
                          {t.description ? <span className="tools-desc">{t.description}</span> : null}
                        </div>
                        <Switch
                          checked={t.enabled}
                          onCheckedChange={(v) => onToggle(t.name, v)}
                          aria-label={`Toggle ${t.name}`}
                        />
                      </div>
                    ))}
                  </div>
                </AccordionItem>
              );
            })}
          </Accordion>
        ) : null}
        {tools.length === 0 ? <p className="muted">No tools match.</p> : null}
      </div>
    </>
  );
}

export function ToolsPanel() {
  return (
    <StagePanel label="tools">
      <ToolsBody />
    </StagePanel>
  );
}

// Work folders as a dialog (ADR 0048) — a labeled chip on the Filesystem group opens the fenced
// fs-roots editor in a dialog, contextual to the tools it governs (the shell/fs policy chip beside
// it does the same for the scalar toggles). Not global chrome above the search anymore.
function WorkFoldersButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="ghost" size="sm" type="button" onClick={() => setOpen(true)}>
        <FolderTree size={15} /> Work folders
      </Button>
      {open ? (
        <Dialog open onClose={() => setOpen(false)} title="Work folders" width={520}>
          <FsProjectsEditor />
        </Dialog>
      ) : null}
    </>
  );
}

// Work-folder editor for the fenced fs roots (`filesystem.projects`, ADR 0007) — the fenced
// roots list (the shell/fs policy chip covers the scalar toggles). Replace-list semantics, same
// as the MCP servers editor. Saving with any folder present also enables fs tools.
function FsProjectsEditor() {
  const toast = useToast();
  const query = useQuery({ queryKey: ["fs-projects"], queryFn: () => api.fsProjects() });
  const [rows, setRows] = useState<FsProject[] | null>(null);
  useEffect(() => {
    if (query.data && rows === null) setRows(query.data.projects);
  }, [query.data, rows]);

  const save = useMutation({
    mutationFn: (projects: FsProject[]) => api.setFsProjects(projects),
    onSuccess: (res) => {
      setRows(res.projects);
      void query.refetch();
      toast({ tone: "success", title: "Folders saved", message: "Filesystem tools cover the listed folders." });
    },
    onError: (e: Error) => toast({ tone: "error", title: "Couldn't save folders", message: errMsg(e) }),
  });

  if (query.isLoading || rows === null) return null;
  const saved = JSON.stringify(query.data?.projects ?? []);
  const dirty = JSON.stringify(rows) !== saved;

  return (
    <div className="fs-projects">
      <p className="fleet-section-label">Work folders</p>
      <p className="setup-hint">
        The folders the filesystem tools may read{" "}
        (and, per-folder, write). The agent can't reach anything outside this list.
      </p>
      {rows.map((row, i) => (
        <div key={i} className="fs-projects-row">
          <Input
            value={row.path}
            placeholder="~/Documents"
            aria-label="Folder path"
            onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, path: e.target.value } : r)))}
          />
          <label className="fs-projects-write">
            <Switch
              checked={row.write}
              onCheckedChange={(v: boolean) => setRows(rows.map((r, j) => (j === i ? { ...r, write: v } : r)))}
            />
            <span>write</span>
          </label>
          <Button variant="ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove folder">
            ✕
          </Button>
        </div>
      ))}
      <div className="fs-projects-actions">
        <Button onClick={() => setRows([...rows, { path: "", write: false }])}>Add folder</Button>
        <Button
          variant="primary"
          disabled={!dirty || save.isPending || rows.some((r) => !r.path.trim())}
          onClick={() => save.mutate(rows)}
        >
          {save.isPending ? "Saving…" : "Save folders"}
        </Button>
      </div>
    </div>
  );
}
