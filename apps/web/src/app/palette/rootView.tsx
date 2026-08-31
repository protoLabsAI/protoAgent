// The HOST-OWNED palette root view (ADR 0057) — the ranked ⌘K list.
//
// UPSTREAM: protoLabsAI/protoContent#503. This module exists ONLY because
// @protolabsai/ui has no ranking seam: `commandsView` renders commands in registration
// order, its matcher is module-private, and the live query is component-local state the
// host cannot reach. The one mechanism the DS does offer is view REPLACEMENT —
// `command-palette.tsx:348-356` builds its view map from `registry.getViews()` and only
// synthesizes a `commandsView` when nothing claims the root id — so registering a view with
// `id: "commands"` takes over filtering, ranking and rendering wholesale. When the DS ships
// `commandsView({ rank })`, this file collapses to a `score` callback and the adoption
// sweep can retire it; keep the issue number attached until then.
//
// Owning the view means owning EVERYTHING `commandsView` did, because it is the only place
// in the DS that calls `CommandProvider.getCommands`. Reimplemented below, in order: the
// registry subscription, the debounced/abortable async provider loop, per-provider `source`
// stamping, source chips, contiguity group headers, wrap-around arrows, Enter-to-run, the
// `disabled` guard, scroll-into-view, autofocus, the spinner/empty swap, and the exact
// `pl-cmdk-commands*` class names + combobox/listbox/option ARIA (two e2e specs assert on
// them: fleet.spec.ts:426-434 and keybindings.spec.ts:11-18).
//
// What it adds — the point of the exercise:
//   • The empty query is a DIFFERENT LIST. `matchCommand` returns true for "", so a root
//     that simply registered every surface would flood the moment you pressed the palette
//     open (which is exactly why the `Open` submorph exists). Empty -> recents first, then
//     a short curated root, capped. Typed -> the FULL corpus, surfaces included, ranked,
//     UNCAPPED.
//   • Selection tracks the selected COMMAND ID, not its index. The DS resets on
//     `[filtered.length]` and scrolls on `[sel]`, which is safe only while order is stable;
//     the instant order depends on the query, a re-rank that preserves the row count leaves
//     the highlight on a different command, off-screen, and Enter runs the wrong thing.
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type {
  Command,
  PaletteContext,
  PaletteRegistry,
  PaletteView,
} from "@protolabsai/ui/command-palette";

import { rankCommands } from "./rank";
import { frecency, markCommandUsed, readPaletteRecency } from "./recents";
import type { RecentMap } from "./recents";
import "../palette.css";

/** Header the recents block renders under. A group name, so it rides the same
 *  contiguity-header path every other section uses rather than a second renderer. */
export const RECENT_GROUP = "Recent";

export type RootViewConfig = {
  /** The registry this view reads. A GETTER because the view is constructed BEFORE the
   *  registry it belongs to — see `createRankedPaletteRegistry`. */
  getRegistry: () => PaletteRegistry;
  /** Rows admitted ONLY once the operator types: the full surface corpus. Re-read per
   *  render, so plugin views appearing/disappearing are picked up without re-registering. */
  searchOnly?: () => Command[];
  /** Rows on the EMPTY query, at most. The only cap in the view. */
  emptyCap?: number;
  /** Recents on the empty query, at most. */
  recentCap?: number;
  placeholder?: string;
  emptyLabel?: string;
  loadingLabel?: string;
  width?: number;
  footerHint?: ReactNode;
  /** Injectable for tests. */
  readRecency?: () => RecentMap;
  onRun?: (c: Command) => void;
  now?: () => number;
};

const SearchIcon = () => (
  <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden>
    <circle cx="7" cy="7" r="4.5" />
    <path d="M11 11l3 3" />
  </svg>
);

/** First write of an id wins — the DS's own precedence (statics are listed first, so a
 *  static beats a provider row claiming the same id). */
function dedupe(commands: Command[]): Command[] {
  const seen = new Set<string>();
  const out: Command[] = [];
  for (const c of commands) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    out.push(c);
  }
  return out;
}

/** The empty-query list: what you reach for, then the curated root. `pool` is everything
 *  addressable (root commands AND search-only surfaces) so a surface you opened yesterday
 *  can be a recent; `root` alone fills the rest, so surfaces never flood it. */
export function emptyQueryList(
  root: Command[],
  pool: Command[],
  recency: RecentMap,
  opts: { emptyCap?: number; recentCap?: number; now?: number } = {},
): Command[] {
  const { emptyCap = 9, recentCap = 4, now = Date.now() } = opts;
  const byId = new Map(pool.map((c) => [c.id, c] as const));
  const recents = Object.entries(recency)
    .filter(([k]) => k.startsWith("cmd:"))
    .map(([k, e]) => ({ cmd: byId.get(k.slice(4)), f: frecency(e, now), t: e.t }))
    .filter((x): x is { cmd: Command; f: number; t: number } => !!x.cmd && !x.cmd.disabled)
    // Last-used breaks the tie — `frecency` underflows to 0 for anything old enough, and
    // an arbitrary order among "everything is stale" reads as a broken list.
    .sort((a, b) => b.f - a.f || b.t - a.t)
    .slice(0, recentCap)
    // A recent row is the SAME command under a different header — cloned so the header
    // swap can't leak into the corpus the search path ranks.
    .map((x) => ({ ...x.cmd, group: RECENT_GROUP }));
  const shown = new Set(recents.map((c) => c.id));
  return [...recents, ...root.filter((c) => !shown.has(c.id))].slice(0, emptyCap);
}

function RootBody({ ctx, config }: { ctx: PaletteContext; config: RootViewConfig }) {
  const registry = config.getRegistry();
  const {
    placeholder = "Search commands, surfaces, agents…",
    emptyLabel = "No matches",
    loadingLabel = "Searching…",
  } = config;

  const [q, setQ] = useState("");
  const [selId, setSelId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const typed = q.trim().length > 0;

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Re-render when the registry changes (commands/providers added or withdrawn).
  const regVersion = useSyncExternalStore(registry.subscribe, registry.getVersion, registry.getVersion);

  // Read the frecency store ONCE per open: the palette body unmounts on close, so a fresh
  // mount is exactly the moment the list should re-learn, and re-reading localStorage on
  // every keystroke would be pointless work in the render path.
  const recency = useMemo(
    () => (config.readRecency ?? readPaletteRecency)(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const now = config.now?.() ?? Date.now();

  // Always-present commands: registry statics + each provider's static commands. Stamped
  // with each provider's source for the chip (`{ source, ...c }` — a command's OWN source
  // wins, so a row that named its origin keeps it).
  const baseCommands = useMemo<Command[]>(() => {
    const providerStatics = registry
      .getProviders()
      .flatMap((p) => (p.commands ?? []).map((c) => (p.source ? { source: p.source, ...c } : c)));
    return [...registry.getStaticCommands(), ...providerStatics];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, regVersion]);

  // Async provider results for the live query (debounced + cancellable) — the DS's loop,
  // with one addition: an empty query short-circuits BEFORE the debounce, so opening the
  // palette paints recents immediately instead of showing the spinner for 120 ms the day a
  // live-search provider is registered.
  const [dynamic, setDynamic] = useState<Command[]>([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const providers = registry.getProviders().filter((p) => p.getCommands);
    if (!typed || providers.length === 0) {
      setDynamic([]);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    let alive = true;
    setLoading(true);
    const t = setTimeout(async () => {
      const settled = await Promise.allSettled(
        providers.map((p) => Promise.resolve(p.getCommands!(q, { signal: ac.signal }))),
      );
      if (!alive || ac.signal.aborted) return;
      const cmds = settled.flatMap((r, i) =>
        r.status === "fulfilled"
          ? r.value.map((c) => (providers[i].source ? { source: providers[i].source, ...c } : c))
          : [],
      );
      setDynamic(cmds);
      setLoading(false);
    }, 120);
    return () => {
      alive = false;
      ac.abort();
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, regVersion, q, typed]);

  const filtered = useMemo(() => {
    const searchOnly = config.searchOnly?.() ?? [];
    if (!typed) {
      return emptyQueryList(baseCommands, [...baseCommands, ...searchOnly], recency, {
        emptyCap: config.emptyCap,
        recentCap: config.recentCap,
        now,
      });
    }
    // Statics first, then the surfaces, then provider rows — the id-dedup precedence the
    // DS uses. Ranking reorders what survives `matchCommand`; it never shrinks it and
    // never caps, so a keyword-only hit (the Fleet Room under a member's name) still lands.
    return rankCommands(dedupe([...baseCommands, ...searchOnly, ...dynamic]), q, {
      score: (id) => frecency(recency[`cmd:${id}`], now),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseCommands, dynamic, q, typed, recency, regVersion]);

  // Selection is by COMMAND ID; the index is derived. A re-rank that keeps the row count
  // (every keystroke that narrows nothing) would otherwise strand the highlight.
  const signature = filtered.map((c) => c.id).join(" ");
  const found = filtered.findIndex((c) => c.id === selId);
  const sel = found >= 0 ? found : 0;
  useEffect(() => {
    setSelId(filtered[0]?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[data-sel="true"]')?.scrollIntoView({ block: "nearest" });
  }, [sel, signature]);

  const run = (c: Command | undefined) => {
    if (!c || c.disabled) return;
    // The ONE place every command runs, whatever contributed it — so the frecency write
    // can't be forgotten by a source. Recorded BEFORE `run`, which may navigate away.
    (config.onRun ?? ((cmd: Command) => markCommandUsed(cmd.id)))(c);
    c.run(ctx);
  };

  const move = (delta: number) => {
    if (!filtered.length) return;
    setSelId(filtered[(sel + delta + filtered.length) % filtered.length].id);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(filtered[sel]);
    }
    // Escape bubbles to the panel (host pops / closes).
  };

  let lastGroup: string | undefined;

  return (
    <div className="pl-cmdk-commands pa-cmdk">
      <div className="pl-cmdk-commands__search">
        <span className="pl-cmdk-commands__search-icon" aria-hidden>
          <SearchIcon />
        </span>
        <input
          ref={inputRef}
          className="pl-cmdk-commands__input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          aria-label={placeholder}
          role="combobox"
          aria-expanded
          aria-controls="pl-cmdk-commands-list"
          autoComplete="off"
          spellCheck={false}
        />
        {loading && (
          <span className="pl-cmdk-commands__spinner" aria-label={loadingLabel} title={loadingLabel} />
        )}
      </div>
      <div className="pl-cmdk-commands__list" id="pl-cmdk-commands-list" role="listbox" ref={listRef}>
        {filtered.length === 0 ? (
          <div className="pl-cmdk-commands__empty">{loading ? loadingLabel : emptyLabel}</div>
        ) : (
          filtered.map((c, i) => {
            const selected = i === sel;
            const showHeader = c.group != null && c.group !== lastGroup;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {showHeader && <div className="pl-cmdk-commands__group">{c.group}</div>}
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  data-sel={selected || undefined}
                  className={[
                    "pl-cmdk-commands__item",
                    selected ? "pl-cmdk-commands__item--sel" : "",
                    c.disabled ? "pl-cmdk-commands__item--disabled" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  disabled={c.disabled}
                  onMouseMove={() => setSelId(c.id)}
                  onClick={() => run(c)}
                >
                  {c.icon != null && (
                    <span className="pl-cmdk-commands__icon" aria-hidden>
                      {c.icon}
                    </span>
                  )}
                  <span className="pl-cmdk-commands__label">{c.label}</span>
                  {c.source?.label != null && (
                    <span className="pl-cmdk-commands__chip">{c.source.label}</span>
                  )}
                  {c.hint != null && <span className="pl-cmdk-commands__hint">{c.hint}</span>}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/** Footer hints. The DS renders its own set from a module-private component, so the markup
 *  is reproduced here to keep the same chrome under a host-owned root. */
function RootHints() {
  const hints: [string, string][] = [
    ["↑↓", "navigate"],
    ["↵", "run"],
    ["esc", "close"],
  ];
  return (
    <div className="pl-cmdk__hints">
      {hints.map(([key, label]) => (
        <span key={label} className="pl-cmdk__hint">
          <kbd className="pl-cmdk__kbd">{key}</kbd>
          {label}
        </span>
      ))}
    </div>
  );
}

/** The ranked root view. `id` is the literal "commands" — the DS's default `rootView` prop,
 *  which both mount sites (App.tsx, Launcher.tsx) leave alone — so registering it IS the
 *  whole handoff: no prop change, no fork of the palette component. */
export function paletteRootView(config: RootViewConfig): PaletteView {
  return {
    id: "commands",
    width: config.width ?? 560,
    footerHint: config.footerHint ?? <RootHints />,
    render: (ctx) => <RootBody ctx={ctx} config={config} />,
  };
}
