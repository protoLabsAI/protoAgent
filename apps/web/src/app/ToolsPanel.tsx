import "./tools.css";

import { Input, Switch } from "@protolabsai/ui/forms";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ChevronUp, Settings2 } from "lucide-react";

import { Accordion, AccordionItem, PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, toolsQuery } from "../lib/queries";
import { InlineSettings } from "../settings/InlineSettings";
import { useUI } from "../state/uiStore";
import { StagePanel } from "./ErrorBoundary";
import { toolGroupHasSettings, toolGroupSettingKeys } from "./toolGroupSettings";

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
        {/* The run_command execution policy + the coarse kill switches used to live up here
            in a QuickSetting chip → dialog (ADR 0048 §2.2). They're settings for ONE group
            of tools, so they now open in place inside that group (#2000) — a panel whose
            every other control is inline shouldn't send you to a modal for three switches.
            Per-tool wiring stays the row switches (a row toggle writes tools.disabled). */}
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
                  {/* Group-level config, expanded in place from the group it governs
                      (#2000). Only groups with settings render it — see toolGroupSettings. */}
                  {toolGroupHasSettings(cat) ? (
                    <ToolGroupSettings
                      category={cat}
                      // filesystem.enabled / allow_run change which tools are BOUND, so the
                      // hot-rebuilt catalog has to be refetched or the list below goes stale.
                      onSaved={() => void queryClient.invalidateQueries({ queryKey: toolsQuery().queryKey })}
                    />
                  ) : null}
                  <div className="tools-list">
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

/** A tool group's own settings, opened in place from the group (#2000) — the same
 *  expand-here-instead-of-a-modal shape as the MCP panel's "Add server". Collapsed by
 *  default: the group's job is listing its tools, and the config is the occasional visit. */
function ToolGroupSettings({ category, onSaved }: { category: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <div className="tools-group-settings">
        <Button type="button" variant="ghost" onClick={() => setOpen(true)}>
          <Settings2 size={14} /> {category} settings
        </Button>
      </div>
    );
  }
  return (
    <div className="tools-group-settings tools-group-settings--open">
      <div className="tools-group-settings-head">
        <span className="tools-group-settings-title">{category} settings</span>
        <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
          <ChevronUp size={14} /> Hide
        </Button>
      </div>
      <InlineSettings keys={toolGroupSettingKeys(category)} onSaved={onSaved} />
    </div>
  );
}

export function ToolsPanel() {
  return (
    <StagePanel label="tools">
      <ToolsBody />
    </StagePanel>
  );
}
