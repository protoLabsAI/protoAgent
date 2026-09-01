import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { chatCommandsQuery, invalidateChatCommands, queryKeys } from "./queries";
import type { SlashCommand } from "./types";

// The composer's `/` menu is ONE shared query now (#3283), where it used to be a bare
// `api.chatCommands()` per ChatSessionSlot mount. Sharing the fetch is the win; keeping the
// old freshness is the constraint, and it is easy to lose by accident because the two are
// traded off by the same option. The per-slot fetch was unconditional, so every newly
// opened chat tab picked up a workflow/skill/plugin command created a moment earlier. A
// query-level `staleTime` silently takes that away: `refetchOnMount` only fires when the
// entry is STALE, so inside the window a freshly mounted tab reads the cached list and the
// new `/command` is simply missing.
//
// These guard both halves — the dedupe the shared key exists for, and the freshness the
// per-slot fetch used to give — plus the invalidation seam that covers the case neither
// can: an already-open tab, whose observer never remounts (#613) and so never refetches on
// its own (no `refetchInterval`, and `refetchOnWindowFocus: false` console-wide).

const rows = (...names: string[]): { commands: SlashCommand[] } => ({
  commands: names.map((name) => ({ name, kind: "workflow", description: "", usage: `/${name}` })),
});

// A client with the console's REAL shared defaults for the options under test
// (lib/queryClient.ts): 5s freshness, no refetch on focus. `retry: false` only so a test
// failure surfaces as a failed assertion rather than a retry storm.
const consoleClient = () =>
  new QueryClient({
    defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: false } },
  });

// Mount an observer on the SHIPPED options (spread, not retyped) with a counting queryFn,
// so these tests bind to whatever freshness policy `chatCommandsQuery()` actually carries.
// `settled` matters: the fetch count ticks when the queryFn STARTS, and a second observer
// mounted while the first request is still in flight rides that request instead of making
// its own — so a test that only waits on the count would pass on a stale-forever query.
function mountSlot(client: QueryClient, count: { fetches: number }, data = rows("deploy")) {
  const observer = new QueryObserver(client, {
    ...chatCommandsQuery(),
    queryFn: async () => {
      count.fetches += 1;
      return data;
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  const settled = () => vi.waitFor(() => expect(observer.getCurrentResult().isFetching).toBe(false));
  return { observer, unsubscribe, settled };
}

const clients: QueryClient[] = [];
const track = (c: QueryClient) => (clients.push(c), c);
afterEach(() => {
  while (clients.length) clients.pop()?.clear();
  vi.useRealTimers();
});

describe("chatCommandsQuery freshness (#3283)", () => {
  it("does not override the console-wide freshness policy — a later chat tab refetches", async () => {
    // The regression this exists for, isolated: with the client's own policy set to
    // "always stale", a second slot mounting must still fetch. It only fails to if the
    // QUERY carries a longer staleTime of its own — which is exactly how a 5-minute window
    // made a new chat tab LESS fresh than the per-slot fetch it replaced.
    const client = track(new QueryClient({ defaultOptions: { queries: { staleTime: 0, retry: false } } }));
    const count = { fetches: 0 };
    const first = mountSlot(client, count);
    await first.settled();
    expect(count.fetches).toBe(1);
    first.unsubscribe();

    // Operator creates a workflow elsewhere, then opens a new chat tab: a fresh observer.
    const second = mountSlot(client, count, rows("deploy", "report-weekly"));
    await second.settled();
    expect(count.fetches).toBe(2);
    expect(second.observer.getCurrentResult().data?.commands.map((c) => c.name)).toContain(
      "report-weekly",
    );
    second.unsubscribe();
  });

  it("a tab opened a minute later refetches under the REAL client defaults", async () => {
    // Same property against the shipped 5s default rather than a synthetic 0, so this
    // still fails if someone re-adds a multi-minute window at either level.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = track(consoleClient());
    const count = { fetches: 0 };
    const first = mountSlot(client, count);
    await first.settled();
    expect(count.fetches).toBe(1);
    first.unsubscribe();

    await vi.advanceTimersByTimeAsync(60_000);

    const second = mountSlot(client, count, rows("deploy", "report-weekly"));
    await second.settled();
    expect(count.fetches).toBe(2);
    second.unsubscribe();
  });

  it("still shares ONE fetch across slots mounting together — the reason the key exists", async () => {
    // The other half: the console opens with a slot per restored session, and the point of
    // the shared key was that they stop firing N identical requests at boot. The client's
    // 5s default covers that without a query-level override.
    const client = track(consoleClient());
    const count = { fetches: 0 };
    const a = mountSlot(client, count);
    const b = mountSlot(client, count);
    const c = mountSlot(client, count);
    await a.settled();
    expect(a.observer.getCurrentResult().data).toBeTruthy();
    expect(count.fetches).toBe(1);
    a.unsubscribe();
    b.unsubscribe();
    c.unsubscribe();
  });

  it("invalidateChatCommands refreshes an ALREADY-OPEN tab — the only trigger that can", async () => {
    // The built-in chat slot is mounted for the app's lifetime (#613), so nothing remounts
    // it: without this call a command created while the tab is open stays missing until a
    // reload, whatever the staleTime is.
    const client = track(consoleClient());
    const count = { fetches: 0 };
    const slot = mountSlot(client, count);
    await slot.settled();
    expect(count.fetches).toBe(1);

    invalidateChatCommands(client);
    await vi.waitFor(() => expect(count.fetches).toBe(2));
    slot.unsubscribe();
  });

  it("invalidates only the focused agent's list (slug namespace, #2887)", () => {
    const client = track(new QueryClient());
    client.setQueryData(queryKeys.chatCommands, rows("deploy"));
    invalidateChatCommands(client);
    expect(client.getQueryState(queryKeys.chatCommands)?.isInvalidated).toBe(true);
  });
});

// ── Source guard: every surface that writes a `/`-command source must refresh the menu ──
//
// `/api/chat/commands` folds FOUR registries into one list (graph/slash_commands.py
// `resolve_slash_commands`): plugin chat commands, workflow recipes, subagents, and
// user-facing skills. Each console surface that creates one invalidates its OWN key —
// `queryKeys.workflows` in Workflow Studio, `queryKeys.playbooks` in the Playbooks editor —
// and none of those is a prefix of `queryKeys.chatCommands`, so the `/` menu is refreshed
// only if the surface says so explicitly. That is the exact call this PR's first cut
// missed for workflows and playbooks; the failure is quiet (the surface itself looks
// perfectly correct), so it needs a guard rather than a convention.
//
// Vite `?raw` globs rather than node:fs, matching statusTokenGuard.test.ts: this tsconfig
// has no node types, and the glob picks up new source files automatically.
const TS_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Keys naming a registry the server resolves into the slash-command list.
const SLASH_SOURCE_KEY = /queryKeys\.(workflows|playbooks|subagents)\b/;

// A surface mutates one of those registries two ways, and BOTH retire a `/` command:
// `invalidateQueries` (refetch the list) and `setQueryData` (patch the cached list in place,
// which a delete does to skip a round-trip).
//
// Counting matters more than presence. The first cut of this guard asked "does this FILE
// mention invalidateChatCommands?", which a file passes as soon as ONE of its paths calls it
// — so PlaybooksSurface, which invalidated on save and forgot on delete, reported clean while
// a deleted playbook's slash command stayed in the shared menu. Presence is the wrong
// question; every write site needs its own refresh, so compare COUNTS per file.
//
// The window is `[\s\S]` and not `[^;]`: a `setQueryData<{ enabled: boolean; playbooks:
// Playbook[] }>(queryKeys.playbooks, …)` carries a SEMICOLON inside its type argument, so a
// semicolon-bounded window stops before ever reaching the key and the write is invisible.
// That is not hypothetical — it is why the first two attempts at this guard reported clean
// against the very delete path they were written to catch.
const SLASH_SOURCE_WRITE =
  /\b(?:invalidateQueries|setQueryData)\b[\s\S]{0,160}?queryKeys\.(?:workflows|playbooks|subagents)\b/g;
const REFRESH_CALL = /\binvalidateChatCommands\s*\(/g;

describe("every writer of a slash-command source refreshes the menu (#3283)", () => {
  it("refreshes the command list once per slash-source write, not once per file", () => {
    const offenders: string[] = [];
    for (const [file, text] of Object.entries(TS_SOURCES)) {
      if (file.endsWith(".test.ts") || file.endsWith(".test.tsx")) continue;
      if (file === "./queries.ts") continue; // where the keys and the helper are DEFINED
      if (!SLASH_SOURCE_KEY.test(text)) continue;
      const writes = (text.match(SLASH_SOURCE_WRITE) ?? []).length;
      if (writes === 0) continue;
      const refreshes = (text.match(REFRESH_CALL) ?? []).length;
      if (refreshes < writes) {
        offenders.push(
          `${file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/lib/")} (${writes} write(s), ${refreshes} refresh(es))`,
        );
      }
    }
    expect(
      offenders,
      "these surfaces write a registry the server folds into /api/chat/commands (via " +
        "invalidateQueries OR a setQueryData patch) more often than they refresh the command " +
        "list — every write path needs its own invalidateChatCommands(qc), not just one " +
        "somewhere in the file (see lib/queries.ts)",
    ).toEqual([]);
  });

  it("the plugin refresh path covers it too", () => {
    // Plugins reach the list through the one shared refresh rather than a key of their own.
    expect(TS_SOURCES["../plugins/usePluginManage.ts"]).toContain("invalidateChatCommands");
  });

  it("the counting guard actually bites when one path forgets", () => {
    // Proof the guard is not vacuous: simulate PlaybooksSurface's real pre-fix state — the
    // save path refreshes, the delete path patches the cache and does not. A presence-based
    // check passes this; the counting check must not.
    const oneSided = `
      void qc.invalidateQueries({ queryKey: queryKeys.playbooks });
      void invalidateChatCommands(qc);
      qc.setQueryData<{ enabled: boolean; playbooks: Playbook[] }>(queryKeys.playbooks, (old) => old);
    `;
    expect((oneSided.match(SLASH_SOURCE_WRITE) ?? []).length).toBe(2);
    expect((oneSided.match(REFRESH_CALL) ?? []).length).toBe(1);
  });
});
