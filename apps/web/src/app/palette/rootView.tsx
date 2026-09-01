// The HOST-OWNED palette root view (ADR 0057) — the ranked command-palette list.
//
// UPSTREAM: protoLabsAI/protoContent#503. This module exists ONLY because
// @protolabsai/ui has no ranking seam: `commandsView` renders commands in registration
// order, its matcher is module-private, and the live query is component-local state the
// host cannot reach. The one mechanism the DS does offer is view REPLACEMENT —
// `command-palette.tsx:348-356` builds its view map from `registry.getViews()` and only
// synthesizes a `commandsView` when nothing claims the root id — so registering a view with
// `id: "commands"` takes over filtering, ranking and rendering wholesale.
//
// RETIREMENT IS NOT AUTOMATIC. `commandsView({ rank })` would let `rank.ts` move upstream,
// but three things below are FIXES to the DS view rather than additions on top of it, and
// handing the root back would silently undo them: the `aria-activedescendant` half of the
// combobox contract, the live regions, and the containment around a provider that throws.
// Take the `rank` seam when it ships — and either land those upstream first or keep this
// file. The issue number stays attached until one of the two happens.
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
//   • Every group is GUARANTEED a row on the empty list (`pickRootFill`). Registration order
//     alone hands the whole list to whoever registered first, which on a first run — no
//     recency to rescue anything — drops Settings and `Open…` off a plugin-heavy console
//     entirely; a per-group ceiling alone stops binding the moment recents shrink the cap.
//   • Group headers are the EMPTY list's, not the ranked one's. The DS's rule is contiguity,
//     which equals grouping only in registration order — the order this view replaces.
//   • Provider rows are ORDERED, never re-filtered, and a broken provider is CONTAINED.
//     Both are contracts the DS states and then leaves to chance; see the two sites below.
//   • The combobox is finished: `aria-activedescendant`, a named listbox, presentational
//     wrappers, and live regions for "No matches" and the result count. Under the DS's
//     markup a screen reader is told a listbox exists and then never told what is in it.
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type {
  Command,
  PaletteContext,
  PaletteRegistry,
  PaletteView,
} from "@protolabsai/ui/command-palette";

import { orderCommands, rankCommands } from "./rank";
import { frecency, markCommandUsed, readPaletteRecency } from "./recents";
import type { RecentMap } from "./recents";
import "../palette.css";

/** Header the recents block renders under. A group name, so it rides the same
 *  contiguity-header path every other section uses rather than a second renderer. */
export const RECENT_GROUP = "Recent";

// ── The four numbers this view is tuned by. Named, exported and asserted in the tests so
// they can be argued about in one place instead of being read out of an expression. ──
/** Rows on the EMPTY query, at most — the only cap in the view. Roughly a screenful at the
 *  340px list height `palette.css` pins, so the list never scrolls before you've typed. */
export const EMPTY_CAP = 9;
/** Recents on the empty query, at most. Under half the list: recents lead it, they do not
 *  BECOME it — a palette that only ever shows what you already ran can't teach you anything. */
export const RECENT_CAP = 4;
/** Rows any ONE group may contribute to the empty list once every group has had its first.
 *  Load-bearing, not cosmetic: the root corpus is Agents(2) → Plugins(N) → Commands(6) in
 *  REGISTRATION order, so a plain `slice(0, EMPTY_CAP)` hands the whole list to whoever is
 *  registered first. Install seven plugin views and the first-run palette becomes two agent
 *  rows and seven plugins, with Settings and `Open…` pushed off the bottom — on the ONE run
 *  where there is no recency to rescue them. The quota is a PASS, not a hard ceiling:
 *  leftover slots are filled in registration order, so a console with no plugins still gets
 *  a full list. It is also not the guarantee — `pickRootFill` sweeps once at a quota of ONE
 *  before this one applies, because a fixed 4 stops binding the moment recents shrink the
 *  cap; see the comment there. */
export const GROUP_CAP = 4;
/** Debounce before the async provider loop fires, in ms. The DS's own figure
 *  (`command-palette.views.tsx`) — kept identical so a provider written against the DS
 *  behaves the same under the host-owned root. */
export const PROVIDER_DEBOUNCE_MS = 120;

export type RootViewConfig = {
  /** The registry this view reads. A GETTER because the view is constructed BEFORE the
   *  registry it belongs to — see `createRankedPaletteRegistry`. */
  getRegistry: () => PaletteRegistry;
  /** Rows admitted ONLY once the operator types: the full surface corpus. A GETTER, read
   *  on every render, because the view object is constructed ONCE (at registry construction)
   *  while the surface set keeps changing as plugins load — a captured array would freeze the
   *  corpus at whatever was resolvable on the first render. The ids it returns feed the memo
   *  key below, so a changed surface set re-ranks even if nothing touched the registry. */
  searchOnly?: () => Command[];
  /** Rows on the EMPTY query, at most. The only cap in the view. Default `EMPTY_CAP`. */
  emptyCap?: number;
  /** Recents on the empty query, at most. Default `RECENT_CAP`. */
  recentCap?: number;
  /** Rows one group may take on the empty query before the rest get a turn. Default
   *  `GROUP_CAP` — see the constant for why a plain prefix of registration order is wrong. */
  groupCap?: number;
  placeholder?: string;
  emptyLabel?: string;
  loadingLabel?: string;
  /** Accessible name for the listbox region. */
  listLabel?: string;
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

/** Pick at most `cap` rows out of the curated root, giving each GROUP a first turn.
 *
 *  Two passes, both in registration order, and the result is emitted in registration order
 *  too — so the rule a reader has to hold is "at most `groupCap` per group first, then
 *  whatever still fits", and the list they see is still the order the adapter registered.
 *  A single pass with a per-group ceiling would starve a console with no plugins (six of
 *  nine slots left empty); a plain prefix starves the LAST group, which is where Settings
 *  and `Open…` live. Exported for the test, which pins both failure modes. */
export function pickRootFill(root: Command[], cap: number, groupCap = GROUP_CAP): Command[] {
  if (cap <= 0) return [];
  const taken = new Set<number>();
  const perGroup = new Map<string, number>();
  /** One sweep in registration order, admitting up to `quota` rows per group. */
  const sweep = (quota: number) =>
    root.forEach((c, i) => {
      if (taken.size >= cap || taken.has(i)) return;
      const g = c.group ?? "";
      const n = perGroup.get(g) ?? 0;
      if (n >= quota) return;
      perGroup.set(g, n + 1);
      taken.add(i);
    });
  // ONE ROW PER GROUP FIRST, and this sweep is the guarantee — `groupCap` alone is not one.
  // The quota only binds while there are more slots than `groupCap * groups`; the moment
  // recents eat into the cap it stops binding entirely and the list reverts to a plain
  // prefix of registration order. With EMPTY_CAP 9 and four recents there are five slots for
  // Agents(2) → Plugins(3) → Commands(6): a `groupCap` of 4 never fires, the first two groups
  // take all five, and the ENTIRE Commands group — `Open…`, `Settings`, every
  // `registerPaletteCommand` deep link, the rows reachable from nowhere else — drops off the
  // list. That is the steady state after day one, not an edge case.
  sweep(1);
  // Then up to the quota, so no single group runs away with a list it shares.
  sweep(groupCap);
  // Last: the quota was a turn-taking device, not a budget. Spend what's left.
  root.forEach((_, i) => {
    if (taken.size >= cap) return;
    taken.add(i);
  });
  return root.filter((_, i) => taken.has(i));
}

/** The empty-query list: what you reach for, then the curated root. `pool` is everything
 *  addressable (root commands AND search-only surfaces) so a surface you opened yesterday
 *  can be a recent; `root` alone fills the rest, so surfaces never flood it.
 *
 *  FIRST RUN is one of the two cases this has to be judged on. There is no recency at all
 *  then, so the whole list is `pickRootFill` — which is why the group quota lives there and
 *  not in some later polish PR: the one run where the list is pure registration order is the
 *  run where a plugin-heavy console would show no Settings and no `Open…`. The other case is
 *  the STEADY state, where recents are subtracted from the cap BEFORE the fill runs and the
 *  quota therefore has far fewer slots to spread — which is why `pickRootFill` guarantees a
 *  row per group rather than relying on `groupCap` to bind. */
export function emptyQueryList(
  root: Command[],
  pool: Command[],
  recency: RecentMap,
  opts: { emptyCap?: number; recentCap?: number; groupCap?: number; now?: number } = {},
): Command[] {
  const { emptyCap = EMPTY_CAP, recentCap = RECENT_CAP, groupCap = GROUP_CAP, now = Date.now() } =
    opts;
  // FIRST id wins, matching `dedupe` and the DS's own precedence. `new Map(pool.map(…))`
  // would quietly give the LAST one — the opposite rule, in the one place a duplicate id is
  // most likely (a provider row shadowing the static it was modelled on).
  const byId = new Map<string, Command>();
  for (const c of pool) if (!byId.has(c.id)) byId.set(c.id, c);
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
  const rest = root.filter((c) => !shown.has(c.id));
  return [...recents, ...pickRootFill(rest, emptyCap - recents.length, groupCap)];
}

function RootBody({ ctx, config }: { ctx: PaletteContext; config: RootViewConfig }) {
  const registry = config.getRegistry();
  const {
    placeholder = "Search commands, surfaces, agents…",
    emptyLabel = "No matches",
    loadingLabel = "Searching…",
    // Distinct from the input's own label. A listbox that borrows the combobox's name is
    // read back as the same thing twice; "Results" is what the region actually is.
    listLabel = "Results",
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

/** Longest a single provider may hold the palette's "Searching…" state.
 *
 *  The containment below turns a synchronous throw and a non-array result into settled
 *  results, but neither covers a promise that simply NEVER settles — a fetch with no
 *  timeout, a dropped socket. `ac.abort()` does not help: aborting only ends the read if the
 *  provider observes the signal, and a fork's provider is under no obligation to. Without a
 *  bound the spinner stays up until the operator types again or closes the palette.
 *
 *  Resolves to an empty result rather than rejecting, so a slow provider degrades to "no
 *  rows from that source" instead of poisoning the round for the others. */
const PROVIDER_DEADLINE_MS = 4_000;

function withDeadline<T>(p: Promise<T>, ms = PROVIDER_DEADLINE_MS): Promise<T | never[]> {
  let timer: ReturnType<typeof setTimeout>;
  const bound = new Promise<never[]>((resolve) => {
    timer = setTimeout(() => resolve([]), ms);
  });
  // Clear on settle so a resolved read does not hold a timer for the full deadline.
  return Promise.race([p, bound]).finally(() => clearTimeout(timer));
}

  // Async provider results (debounced + cancellable) — the DS's loop, with the one timing
  // split the DS never had to make.
  //
  // A SOURCE IS READ ON EVERY PALETTE READ, THE OPEN INCLUDED. That is the contract ADR 0061
  // states and `ext/README.md` sells ("once when ⌘K opens and again on each keystroke"), and
  // it is the whole point of the read-time half of the seam: a source exists for rows that
  // track live data — open chat tabs, a roster — and a fork's rows have to be ON SCREEN when
  // the palette opens, not one keystroke later. So the loop runs for the empty query too.
  // Skipping it there (an earlier revision of this file did) silently deleted every source
  // row from the opened palette and left three shipped docs describing the opposite.
  //
  // What the empty query skips is the SPINNER and the DEBOUNCE, which is all the short-circuit
  // was ever for: the debounce exists to keep a live-search provider from firing per keystroke,
  // and there are no keystrokes before the first one. Firing immediately with `loading` left
  // false means ⌘⇧K still paints recents on the first commit — source rows merge in when they
  // settle — instead of showing "Searching…" for 120ms. Typed queries keep the DS's debounce
  // and spinner exactly.
  const [dynamic, setDynamic] = useState<Command[]>([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const providers = registry.getProviders().filter((p) => p.getCommands);
    if (providers.length === 0) {
      setDynamic([]);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    let alive = true;
    const read = async () => {
      const settled = await Promise.allSettled(
        providers.map((p) => {
          // CONTAINMENT, and the reason owning the view is worth it. A provider that throws
          // SYNCHRONOUSLY throws out of this `.map` — before `allSettled` exists to catch
          // it — so the whole callback rejects: `setLoading(false)` never runs and the
          // palette spins "Searching…" forever, with no row and no error, until the operator
          // closes it. Core's own provider guards itself (`paletteSourceProvider`), but a
          // fork's `registry.registerProvider` is a public DS seam and cannot be made to.
          // Turning the throw into a rejection makes it one more settled result.
          try {
            return withDeadline(Promise.resolve(p.getCommands!(q, { signal: ac.signal })));
          } catch (err) {
            return Promise.reject(err);
          }
        }),
      );
      if (!alive || ac.signal.aborted) return;
      const cmds = settled.flatMap((r, i) =>
        // `Array.isArray` for the same reason: a provider that resolves to a non-array (an
        // object, a bare `undefined` from a forgotten `return`) would throw on `.map` here
        // — inside an async callback nothing awaits — and strand the spinner identically.
        r.status === "fulfilled" && Array.isArray(r.value)
          ? r.value.map((c) => (providers[i].source ? { source: providers[i].source, ...c } : c))
          : [],
      );
      setDynamic(cmds);
      setLoading(false);
    };
    if (!typed) {
      // Cleared BEFORE the read, not inside it. `read` only lowers the flag after its
      // `await`, and the empty query is also reached by DELETING a query that was in
      // flight — where `loading` is already true, so that one awaited tick would commit a
      // "Searching…" frame over the recents list on the way back to it.
      setLoading(false);
      void read();
      return () => {
        alive = false;
        ac.abort();
      };
    }
    setLoading(true);
    const t = setTimeout(read, PROVIDER_DEBOUNCE_MS);
    return () => {
      alive = false;
      ac.abort();
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, regVersion, q, typed]);

  // Read the surface corpus every render (it is a ref read behind the getter, not work) and
  // key the memo on its ids. Before this the memo's only surface-shaped dependency was
  // `regVersion`, which happens to bump when the adapter re-registers its views — true today,
  // and an invisible coupling to another module's effect ordering the day it isn't.
  const searchOnly = config.searchOnly?.() ?? [];
  // id AND label — the adapter's own `navSig` keys on both, because a surface can be RENAMED
  // without its id moving and a sig that ignored the label would leave the old title ranked
  // and rendered until something unrelated invalidated the memo.
  const searchOnlySig = searchOnly.map((c) => `${c.id}\u0000${c.label}`).join("|");

  const filtered = useMemo(() => {
    // ONE dedupe, both paths. It used to run on the typed path only, which left the empty
    // list free to render two rows with the same `c.id` — and `key={c.id}` on the row means
    // React reconciles them as one: a duplicate-key warning, and a highlight that jumps.
    //
    // Two corpora, and keeping them apart IS the feature: `root` is what the empty list may
    // FILL from (registered commands + a source's live rows), `local` is everything
    // addressable and is only ever the recents lookup pool / the search corpus. Fold the
    // surfaces into `root` and the empty palette dumps every rail surface — the flood the
    // whole split exists to prevent.
    //
    // `dynamic` belongs to the EMPTY corpora and not to `local`, and the asymmetry is
    // deliberate on both sides. It is IN the empty ones because a source's rows are read on
    // open (see the loop above) and must therefore be listable there — and because a source
    // row that gets run writes `cmd:<id>` frecency, which is unreadable unless the same id
    // can be resolved out of the recents `pool`. It is OUT of `local` because `local` is
    // what `rankCommands` CLIENT-FILTERS, and a provider's rows must never be re-filtered
    // (they take the `orderCommands` path below instead — see `orderCommands`).
    const root = dedupe([...baseCommands, ...dynamic]);
    const local = dedupe([...baseCommands, ...searchOnly]);
    if (!typed) {
      return emptyQueryList(root, dedupe([...local, ...dynamic]), recency, {
        emptyCap: config.emptyCap,
        recentCap: config.recentCap,
        groupCap: config.groupCap,
        now,
      });
    }
    const score = (id: string) => frecency(recency[`cmd:${id}`], now);
    // Statics first, then the surfaces, then provider rows — the id-dedup precedence the
    // DS uses. Ranking reorders what survives `matchCommand`; it never shrinks it and
    // never caps, so a keyword-only hit (the Fleet Room under a member's name) still lands.
    //
    // The two halves take DIFFERENT paths on purpose. `rankCommands` filters+orders the rows
    // the DS itself client-filters. Provider rows are only ORDERED: a provider is a remote
    // search that already applied the query its own way, so the DS appends its results
    // verbatim, and re-filtering them here would silently delete a fork's fuzzy/semantic
    // hits — the exact rows a source exists to contribute. See `orderCommands`.
    return dedupe([
      ...rankCommands(local, q, { score }),
      ...orderCommands(dynamic, q, { score }),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseCommands, dynamic, q, typed, recency, regVersion, searchOnlySig]);

  // Selection is by COMMAND ID; the index is derived. A re-rank that keeps the row count
  // (every keystroke that narrows nothing) would otherwise strand the highlight.
  //
  // The DOM id is by INDEX, not by command id: it is only ever `aria-activedescendant`'s
  // pointer, a command id can be anything a plugin author typed, and one root view is
  // mounted at a time so the ids cannot collide.
  const signature = filtered.map((c) => c.id).join(" ");
  const found = filtered.findIndex((c) => c.id === selId);
  const sel = found >= 0 ? found : 0;
  // Two DIFFERENT things re-rank the list, and they want opposite selection behavior — which
  // is why this is two effects rather than one rule keyed on `signature`.
  //
  //  • The QUERY changed. A new query is a new ranking, so the highlight belongs on the new
  //    best match: type "bee" then "queen" and the leader flips even though the row COUNT is
  //    identical. Keying only on `signature` (or, as the DS does, on `filtered.length`) leaves
  //    the highlight on row 0 of a list whose row 0 is now a different command.
  //  • Provider rows MERGED IN at the same query. That lands asynchronously — after the
  //    debounce on a typed query, and after the empty-query read on open — so resetting here
  //    yanks the highlight back to row 0 out from under an operator who has already pressed
  //    ArrowDown, and Enter runs the first row instead of the selected one.
  //
  // So: reset on the query, preserve across a same-query re-rank, and never strand the
  // selection on a row that left the list.
  useEffect(() => {
    setSelId(filtered[0]?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);
  useEffect(() => {
    setSelId((cur) => (cur !== null && filtered.some((c) => c.id === cur) ? cur : (filtered[0]?.id ?? null)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>('[data-sel="true"]')?.scrollIntoView({ block: "nearest" });
  }, [sel, signature]);

  const run = (c: Command | undefined) => {
    if (!c || c.disabled) return;
    // The ONE place every row THIS VIEW renders runs, whatever contributed it — so the
    // frecency write can't be forgotten by a source. Recorded BEFORE `run`, which may
    // navigate away. A SUBMORPH (`Open ▸`, the Fleet Room) is a different view with its own
    // list and does not pass through here; the adapter wraps those lists in `withRecency`.
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
  const optionId = (i: number) => `pl-cmdk-opt-${i}`;

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
          // The half of the combobox contract the DS never wired. Focus never leaves the
          // input — arrows move a `data-sel` class — so WITHOUT this a screen reader
          // announces the field once and then says NOTHING as the operator arrows down a
          // list they cannot see. `aria-selected` alone is not announced; the pointer is.
          aria-activedescendant={filtered.length ? optionId(sel) : undefined}
          autoComplete="off"
          spellCheck={false}
        />
        {loading && (
          <span className="pl-cmdk-commands__spinner" aria-label={loadingLabel} title={loadingLabel} />
        )}
      </div>
      <div
        className="pl-cmdk-commands__list"
        id="pl-cmdk-commands-list"
        role="listbox"
        aria-label={listLabel}
        ref={listRef}
      >
        {filtered.length === 0 ? (
          // `role="status"` so the swap to "No matches" is ANNOUNCED. It is the one state
          // with nothing to point `aria-activedescendant` at, so without a live region a
          // screen-reader operator gets silence and has no way to tell an empty result from
          // a palette that stopped responding.
          <div className="pl-cmdk-commands__empty" role="status">
            {loading ? loadingLabel : emptyLabel}
          </div>
        ) : (
          filtered.map((c, i) => {
            const selected = i === sel;
            // HEADERS BELONG TO THE EMPTY LIST ONLY. `c.group !== lastGroup` is the DS's
            // contiguity rule, and contiguity means "grouping" only while the list is in
            // REGISTRATION order — which the empty list is and the typed list, by
            // construction, is not. Ranking sorts by match tier ACROSS groups, so one
            // group's rows are scattered through the result and the same header re-emits at
            // every transition: on the default console, typing `t` produced 8 headers over
            // 16 rows, "Commands" three times. Dropping them on the typed path is the honest
            // reading as well as the fix — a relevance-ordered list has no sections, and a
            // section title over rows that are not a section is worse than none.
            const showHeader = !typed && c.group != null && c.group !== lastGroup;
            lastGroup = c.group;
            return (
              // `presentation` on the wrapper and the header: a listbox may only own
              // options, and an unlabelled generic between the two breaks the owns
              // relationship — some AT then reports "0 items". The header text survives; it
              // is just no longer a node in the listbox's own tree.
              <div key={c.id} role="presentation">
                {showHeader && (
                  <div className="pl-cmdk-commands__group" role="presentation">
                    {c.group}
                  </div>
                )}
                <button
                  type="button"
                  role="option"
                  id={optionId(i)}
                  aria-selected={selected}
                  aria-disabled={c.disabled || undefined}
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
      {/* The result COUNT, announced politely on every settled query. `aria-activedescendant`
          reports the row you are on; nothing reports how many there are, and "did that
          narrow anything?" is the question a sighted operator answers at a glance. Rendered
          empty while the list is empty so the `role="status"` above owns that message
          instead of both firing at once. */}
      <div className="pa-cmdk__sr" role="status">
        {typed && filtered.length > 0
          ? `${filtered.length} ${filtered.length === 1 ? "result" : "results"}`
          : ""}
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
