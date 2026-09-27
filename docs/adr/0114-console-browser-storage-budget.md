# 0114: Console browser storage — transcripts in IndexedDB, a budget, and a full quota never crashes

Status: **Proposed**

Amends: ADR 0104 (the primary transcript store moves from `localStorage` to IndexedDB)
and ADR 0035 D5 (layout persistence goes through the storage seam). Palette/DM thread
persistence had no ADR; this one now governs it.

## Context

On 2026-09-27 the console fell through to the root error boundary (`AppCrash`) with
`The quota has been exceeded.` (`QuotaExceededError`), and **Reload** brought the same
error back. That message is WebKit/Firefox wording (Chromium says "Setting the value
of '…' exceeded the quota"), so the crash was most likely in the desktop WKWebView or in
Safari. We audited every browser-storage writer in `apps/web/src`, then ran three
adversarial review passes over the first draft of this ADR. The findings:

**1. One origin, one ~5 MB quota, many agents and backends.** `localStorage` is
capped at about 5 MB per *origin*. These all share one origin:

- every fleet member opened through the hub (`/app/agent/<slug>/`, proxied at
  `/agents/{slug}/…`). Managed members run with `--ui none`, so the hub origin is
  the only way to reach them;
- every desktop window, whichever backend it talks to (`tauri://localhost`,
  `index.html?__apiPort=<port>`, `apps/desktop/src-tauri/src/lib.rs:1236-1245`), and
  ADR 0113 paired remotes on top;
- plugin iframes, which are `allow-same-origin` (`app/PluginView.tsx:645`).

Keys carry a per-slug suffix, so usage grows with every agent ever visited. Nothing
ever removes old ones. Desktop windows all resolve to slug `host`, whichever backend
they are on.

**2. The chat transcript store has no size limit.** `protoagent.chat.sessions[:<slug>]`
(`chat/chat-store.ts:99`) caps the session count at 50 and nothing else. The persisted
message stores reasoning twice (`reasoning` and `parts`) and component specs twice
(`components[]` and `parts`, with uncapped props). Tool `input` is uncapped too.
`protoagent.palette.chat[:<slug>][:dm:<member>]` has no cap at all. Every persist
parses, merges and re-stringifies the whole blob, up to every 300 ms while a turn
streams.

**3. The biggest writer fails silently.** `persist()` (`chat-store.ts:504-538`)
swallows every error. Once the quota is full, history stops saving and the operator
is never told.

**4. Small writers with no error handling are what crash.** zustand 5 `persist`
calls `storage.setItem` synchronously inside every `set()`, no-op sets included,
with no catch (`zustand/esm/middleware.mjs:356-373`). The affected stores are
`useUI`, `createUISlice` and `useKeybindingOverrides` (the last has no `storage:`
option, so it falls back to raw `window.localStorage`). The worst path is `useUI`
setters running in mount effects (`App.tsx:476`, `:832-834`
`reconcileCoreSurfaces`, `:895-900`): they throw into the root boundary on every
reload. Throws from event subscriptions don't reach `AppCrash`, but they still
break things silently. `events.ts:105-107` `dispatch` doesn't isolate listeners, so
one throwing `setPluginDot` subscriber stops delivery to every listener after it.

**5. Recovery is incomplete.** `AppCrash`'s reset leaves palette and DM threads in
place. The crashed page's `pagehide` flush can write the chats straight back, and
other tabs re-merge their in-memory copy.

## Decision

Browser storage is a **budgeted, best-effort cache**. No render and no subscription
may depend on a storage write succeeding. The only large data, transcripts, moves to
IndexedDB (IDB). Six rules follow.

### D1: One storage seam; writes never throw by default

All `localStorage`/`sessionStorage` access goes through `apps/web/src/lib/storage.ts`.

- **Functions.** `readKey(area, key): string | null`,
  `writeKey(area, key, value): {ok: true} | {ok: false, reason: "quota" | "unavailable"}`,
  `removeKey(area, key)`, and `writeKeyStrict`, which throws.
- **Credential writes use `writeKeyStrict`.** The auth-token writes
  (`DevicesPanel.tsx:224`, `pairing.ts:73`) must not report success after failing
  to store the token. Otherwise the server gets a token the browser never kept.
  Both sites are already inside a `try` that surfaces the error.
- **The tenant uid is not a credential.** `tenant.ts` runs inside `TenantGuard`'s
  effect, where a strict throw would bring back the crash. It uses `writeKey`.
- **zustand adapter.** `persistStorage(area)` is the adapter for zustand's
  `createJSONStorage`. Its `getItem` returns exactly `string | null`, and its
  `setItem` never throws.
  - The adapter must work on zustand's migrate-on-hydrate path, where `setItem` runs
    inside hydration.
  - `useUI`, `createUISlice` and `useKeybindingOverrides` all use it.
  - `useUI`'s wrapper skips the write when the partialized state is unchanged
    (compared by value to the last write), so no-op sets and `pluginDots` toggles
    stop rewriting the layout blob.
- **Quota detection.** A write counts as over quota when `name ===
  "QuotaExceededError"`, `name === "NS_ERROR_DOM_QUOTA_REACHED"`, or `code` is 22
  or 1014.
- **On a `localStorage` quota error.** Run eviction (D5) once, retry once, then
  **latch** `failing`. While latched, writes to evictable categories return
  `quota` without touching storage. The latch is released by a removal, an
  eviction, or a `storage` event. `sessionStorage` has its own per-tab quota; it
  never triggers eviction and only reports the failure.
- **Listener isolation.** `events.ts` `dispatch` wraps each listener in its own
  try, so one throw can't starve the others or skip the `lastSeq` update.
- **Enforcement** is a vitest source guard, in the same style as
  `app/statusTokenGuard.test.ts`, not ESLint (the console has none). It uses
  `import.meta.glob("?raw")` and fails on:
  - any `\b(localStorage|sessionStorage)\b` outside `lib/storage.ts` and a short
    allowlist (`AppCrash.tsx`, tests);
  - any zustand `persist(` without a `storage:` option;
  - any `\bindexedDB\b` or `from "idb"` outside `chat/transcriptStore.ts`.
- **Key registry.** `storage.ts` holds a typed **key registry**. Each entry records:
  - the key pattern;
  - its category (`auth | tenant | theme | layout | prefs | transcript | index |
    dismissals | ephemeral`);
  - a slug extractor;
  - whether it is evictable.

  Matching is by exact pattern, never by prefix, so `protoagent.chat.sessions:<slug>.dismissed`
  (ADR 0104's dismissal set) is never mistaken for a transcript. Unregistered keys
  (plugins, design system) are counted but never touched.

### D2: Transcripts live in IndexedDB, and IDB is the source of truth

`localStorage` keeps only small state that boot needs synchronously: layout, auth,
tenant, theme, flags, keybindings, and a derived index. Chat and palette/DM
**transcripts** move to IDB, accessed through `chat/transcriptStore.ts` (built on
`idb`, ~1 KB; the library never leaks past this module).

**Database per backend.** One database per backend:
`protoagent:<tenantUid>`, keyed by the hub's data-root uid (`lib/tenant.ts`). Stores:

- `sessions`, keyed `[slug, sessionId]`: `{rev, updatedAt, meta, messages}`, where
  `meta` is title, model, effort, bypass, incognito and participants;
- `threads`, keyed `[slug, scope]`: palette/DM messages. The thread's `contextId`
  stays in `localStorage`, so the server link survives a slow or failed IDB read;
- `tombstones`, keyed `[slug, sessionId]` with a `deletedAt`, pruned after 30 days.

This scoping fixes three problems:

- A tenant switch opens a different database instead of racing a blocked
  `deleteDatabase`.
- Two desktop backends on `tauri://localhost` can't see or evict each other's
  transcripts.
- The previous backend's history can't show up under the new one.

A database belonging to another uid is never deleted automatically, because it may
belong to a live backend in another desktop window. The only way to clear one is
Settings → Storage.

**Boot-time uid.** The uid arrives over the network (`App.tsx` `hostUidQ`), but the
chat store loads at module init, so boot uses the **last verified uid for this
API base**.

- The key is `protoagent.tenant.uid:<apiBase>`, where `apiBase` is the origin plus
  the desktop `__apiPort`. Desktop windows on different backends therefore don't
  share it. Today's single key, `protoagent.tenant.uid`, does get shared, and it
  already causes wipe-and-reload ping-pong.
- **No stored uid** (first visit): transcripts stay `pending` until the runtime
  status answers.
- **A backend that reports no uid** (older builds) uses `protoagent:_legacy`.
- **On a mismatch**, `TenantGuard` stores the new uid and reloads, so the new
  database opens.
- **`tenantCheck`'s prefix wipe of `protoagent.chat.sessions*` is removed in slice
  5.** It would delete v1 blobs before they migrate, along with ADR 0104's
  `.dismissed` sets. Per-uid databases make the wipe unnecessary.

**Derived index.** `protoagent.chat.index:<tenantUid>:<slug>` in `localStorage` holds
`[{id, title, updatedAt, hasMessages, incognito, liveTaskId?}]` and is a few KB per
agent.

- It is a cache of IDB, not a peer. Writes read, merge and write **per session id**,
  honouring tombstones. They never replace the whole list.
- After first paint, the store walks the slug's IDB records and reconciles:
  - adopt any record that has no index row;
  - drop any row that has no record and no pending write.
- It drives the synchronous surfaces:
  - the tab strip;
  - S2 live-turn resume (`sessionsWithLiveTurns`, `chat-store.ts:651`);
  - `FleetTurnWatch`, which switches to reading the index **in the same slice as
    the migration**, treating a missing key as "unknown", not "finished".
- `currentSessionId` stays per-tab, as it is today (last writer wins on reload).

**The load barrier.** This is the core contract, and it is what prevents
destructive overwrites.

- **Load states.** Each session has a `loadState`: `pending | loaded | failed`.
  - `pending` times out to `failed` after 10 s.
  - A read that resolves late moves `failed` to `loaded` and drains the queue.
  - Sessions created locally (`createSession`, fork) start out `loaded`.
- **Updater-only mutators.** Every message mutator takes an **updater function**,
  `(messages) => messages`, and the array-replacing form is removed. Callers that
  move to updaters:
  - the scheduled, background, resume and server-turn watches;
  - `reattach.ts`, `exportChat.ts` and `publishChat.ts`;
  - `chat/coreSlashCommands.ts`, including `/compact`'s `[summary, ...kept]`;
  - every `ChatSurface.tsx` site: clear, rewind, fork seed, dismiss and the stream
    loop.
- **No branching on the snapshot.** Callers must not branch on a snapshot before
  calling the mutator. The updater treats a missing target as a no-op.
- **Queued updaters.** The store queues updaters for a session that isn't `loaded`
  and applies them after the load. For a `failed` session, queued updaters apply to
  the in-memory view only. They are shown as unsaved and retried on
  `pageshow`/`visibilitychange`.
- **User-initiated edits wait for `loaded`.** Send, regenerate, rewind, fork,
  dismiss and slash commands are disabled until the session is `loaded`.
- **No `put` ever runs for a session that isn't `loaded`.**
- `failed` is never read as "empty":
  - server hydration may show its turns read-only in the UI but never persists
    over an unread record;
  - `unusedSession`, the `createSession` reuse guard and
    `mergeHydratedSessions`'s placeholder removal require `loaded && messages.length
    === 0`, or `!hasMessages` from the index.
- Reattach and `reconcileSessionStatus` wait for `loaded`. Until then, the index's
  `liveTaskId` alone keeps a session `streaming` and the composer locked.
- A `failed` session still gets a status-only reconcile, which writes no messages,
  so its composer can't stay locked forever.
- PaletteChat gets the same gate: no save, self-heal or `initial` auto-send before
  its thread is `loaded`.

**Write path.**

- Streaming deltas stay debounced at 300 ms. Other changes write immediately.
- `persist` diffs sessions by reference against the last-persisted snapshot, so
  non-transcript state changes write nothing to IDB.
- Each write is one `readwrite` transaction: `get`, check `rev`, then `put` with
  `rev + 1`.
  - If the stored `rev` is newer than expected (another tab wrote first), merge by
    id union using the cross-tab rule below, then put `stored.rev + 1`.
  - Settings and rename are **patches** of `meta` inside that transaction, never a
    full put from a copy that might be stale.
  - `liveTaskId` is cleared from the index only after the put transaction completes.
    Live status is derived from index **or** record.
- `createSession`'s `slice(-MAX_SESSIONS)` (still 50) deletes the dropped records
  as well.

**What `pagehide` can lose.** `pagehide` flushes, but an IDB transaction started
there isn't guaranteed to commit. At risk is anything written in the last ≤300 ms
that hasn't committed. For streamed text, the server holds it and S2's reattach
restores it. For client-only rows the loss is real but tiny: a system note, a
scheduled card, the local cut after a rewind, or a settings change.

**Cross-tab.** A `BroadcastChannel("protoagent:<tenantUid>")` message
`{slug, sessionId, rev, deleted?}` replaces the `storage`-event merge.

- A receiver re-reads the record when `rev` is newer, unless it is streaming that
  session. It merges messages **by id union**, keeping local messages older than
  any incoming trim marker, so a trimmed record can't replace a fuller live
  transcript.
- `deleted` adds the id to `locallyDeletedIds` and the persisted tombstones.
- Tabs re-validate against IDB on `pageshow` and `visibilitychange`, because frozen
  or bfcached tabs miss broadcasts.
- Every connection `close()`s on `versionchange`.

**Migration (one shot, merge-safe).** On boot, each v1 key that matches the
registry's exact transcript pattern is migrated:

1. Parse it through `sanitizePersisted`.
2. Inside one transaction, `put` each session only if the record is absent, or if
   it is older and not tombstoned.
3. Write the index.
4. Remove the v1 key after the commit.

A tab still on the old build may rewrite the v1 key. A `storage` listener on v1 keys
stays in place through the transition and re-migrates by **merging by id union**
into the record through the `rev` transaction. It never replaces the record, because
the old tab's copy can have a newer `updatedAt` while missing messages the new tab
appended. The migration honours ADR 0104's dismissal set. v1 keys are never evicted
while a newer build is migrating them. Palette and DM keys migrate
the same way, except that `contextId` stays in `localStorage`. The migration is
**one-way**: an older console on this origin sees only ADR 0104's 24 h server
rebuild.

**Validation.** Every IDB read goes through `sanitizePersisted`, not just the
migration. Records are same-origin and plugin-writable, so a corrupt one is dropped
and logged, never rendered (the #872 class).

**If IDB is unavailable** (it fails to open, e.g. in locked-down profiles):

- the store runs in memory and D6 shows the banner;
- v1 keys are read in **read-only** mode and never deleted, so existing history
  still shows.

### D3: Each persisted item has a size limit, and nothing is derived that isn't equivalent

- **`content` is always stored.** It isn't derivable from `parts`, for several
  reasons:
  - server-hydrated messages, system notes, room bubbles, error text and palette
    self-heal messages are content-only;
  - `content` keeps inter-call paragraph breaks that `parts` drops;
  - rewind and fork match the server's `_content_text` exactly
    (`graph/rewind_op.py:135`).
- **The compact form (record `v2`) drops only duplicates verified at write time:**
  - `reasoning`, when the concatenated reasoning runs in `parts` equal `reasoning`
    byte for byte;
  - `components`, when every spec is present in `parts` with an identical
    serialization.

  Both are re-derived on load.
- **Single values:**
  - tool `input` over 64 KB is truncated with a marker;
  - a component spec over 64 KB is replaced whole by a "component too large to
    keep" placeholder spec. A spec is never truncated, because a half-spec breaks
    the renderer.
- **Dismissal sets** store `{id, at}` and are pruned after 48 h, well past the 24 h
  server window ADR 0104 hydrates from. They are never capped by count.
- **Message caps, which apply to the persisted payload only and never to an open
  tab's live transcript.** From the slice that introduces them, `mergeSessions`
  becomes an id-union merge that keeps local messages older than an incoming
  marker, so a trimmed copy from another tab can't replace a fuller live session.
  The caps are:
  - `MAX_PERSISTED_MESSAGES = 200` per chat session;
  - `100` per palette/DM thread.

  How the cut is made:
  - Cuts fall only on turn boundaries: never inside a `splitOf` group, between a
    prompt and its reply, or inside an addressed exchange.
  - One trim marker per session replaces the dropped prefix. It has a stable id and
    `role: "system"`, so rewind, regenerate and dedupe skip it.
  - The marker is a **pure function of live state**: the live marker's counts, if
    any, plus the live messages this write drops. It is never derived from the
    previously persisted record, so a re-trim can't double-count. The marker
    carries:
    - `droppedCount`;
    - `droppedContentCounts` (key → n), which rewind/fork/regenerate add to their
      occurrence count (`ChatSurface.tsx:2128,2168,2222`), so the server still cuts
      at the right message. The key is the FNV-1a 64-bit hash of
      `(m.content ?? "").trim()` over every dropped message of every role. That is
      exactly the client's occurrence filter; it is computed synchronously, without
      `crypto.subtle`, which isn't available over http. A fixture proves each
      duplicate position across the cut still resolves to the same server message
      (`graph/rewind_op.py` `_resolve_end`);
    - `droppedAuthors`, which feeds `sessionCast`.
  - The marker row never offers rewind or fork.
  - Marker text: "Earlier messages were removed from this browser". It makes no
    promise about `/export`, which renders a checkpoint that compaction rewrites
    and that never held client-only rows.

### D4: Liveness, meaning which slugs are in use

A slug is **live** if any of these hold:

- it is `host` or the bare key;
- it is in the current backend's fleet list. That list counts only when it comes
  from a 200 response that includes a host entry; a 403, 404, error or empty list
  means "unknown";
- another window has it open. Presence is checked with a BroadcastChannel ping,
  `{type: "who", slug}`: any window on that slug answers within 250 ms.
  BroadcastChannel works without a secure context. Web Locks (`navigator.locks`)
  do not, and the console is commonly served over plain http on LAN/tailnet
  addresses, so locks are not relied on.
- if nothing answers but the slug was written in the last 24 h, it is still
  treated as live. This stops eviction from thrashing against a tab that is frozen
  or asleep and would write the data back.

Slug extraction always goes through the key registry, never by parsing suffixes.
There is **no proactive stale-slug garbage collection**. Liveness is only consulted
under real quota pressure (D5). On `tauri://` origins, `localStorage` keys that
aren't scoped by tenant uid are never evicted by liveness.

### D5: Budgets and eviction

The quota error is authoritative. The budget is a heuristic that exists so eviction
happens before the error.

**`localStorage`: 4 MB of UTF-16 bytes** (about 2M characters). Usage is measured by
a full scan at boot and after every quota error, which counts plugin and design-system
keys too, and then tracked by deltas. Once past the budget, or on a quota error,
eviction proceeds in this order:

1. transcript keys (v1 blobs and palette/DM threads) of slugs that aren't live,
   least recently written first;
2. before the IDB slice ships, transcript blobs of fleet slugs that **no open window
   answers for and that weren't written in the last 24 h**, least recently written
   first. These blobs are the realistic hog, and they are
   recoverable from the server's 24 h window;
3. `proto:uislice:*` and layout keys of slugs that aren't live.

Never evicted: auth, tenant, theme, keybindings, dismissal sets, the current and
`host` layout, the index, and the keys of any slug an open window answers for.

**IDB.** There is no proactive percentage budget. On an IDB `QuotaExceededError`:

1. evict records of slugs that aren't live;
2. then palette/DM threads, least recently written first;
3. then chat sessions, least recently updated first.

Eviction removes the index row too, and broadcasts a deletion. It never touches a
present slug's open sessions or anything with a `liveTaskId`.
`navigator.storage.estimate()` is used only for display, when present (it needs a
secure context and Safari 17+). `navigator.storage.persist()` is requested only from
a Settings → Storage click. It is feature-detected, and Firefox shows its prompt only
then.

### D6: Pressure is visible

The seam and `transcriptStore` publish one signal:
`{state: "ok" | "evicted" | "failing", lastEvicted, usage}`. Transitions are also
logged to the client log, so a bug report carries them.

- **`evicted`:** a one-time toast: "Freed browser storage: removed N old chats from
  this browser".
- **`failing`:** a persistent chat banner, worded neutrally because private windows
  hit it legitimately: "Chat history isn't being saved in this browser". It has a
  **Free up space** action that runs eviction on demand and a **Manage storage**
  link.
- **Settings → Storage**, in the "This console" group (`settings/sections.ts`
  `CONSOLE_SECTIONS`, plus its icon and a `sections.test.ts` pin):
  - usage by agent and category, from the key registry plus each database;
  - per-row Clear;
  - clearing another backend's database;
  - "keep data" (`persist()`).
- **`AppCrash`** recognises the quota error by the names and codes above and offers
  **Free up space & reload**. After D1, only a `localStorage` quota error can reach a
  render boundary, so this action:
  - sets an in-realm flag (`globalThis.__protoagentNoFlush`) and cancels the chat
    store's pending timer through that flag, without importing the store;
  - clears only `localStorage` transcript categories through the registry, never
    IDB, then runs `localStorage` eviction step 3;
  - if that freed less than 64 KB (after slice 5 the hog may be plugin or
    design-system keys), shows the largest `localStorage` keys by size, including
    unregistered ones, each with its own Clear, instead of looping through reload;
  - broadcasts `{type: "storage-reset"}` so other tabs drop dirty state and reload;
  - reloads.

## Considered options

- **Only add try/catch.** This stops the crash, but chat still stops saving silently
  and the quota still fills. We kept it as D1, but it isn't sufficient on its own.
- **Budget `localStorage` and keep transcripts there.** One 5 MB quota shared by the
  whole hub and every desktop backend makes eviction routine, and every write still
  re-serializes a whole blob. Rejected.
- **Make the server the transcript store** (raise the 24 h TTL, or add a history
  table). ADR 0104 rejected a fourth history record, and TTL depth is a product
  decision. Not reopened here.
- **Derive `content` from `parts`.** Not equivalent (see D3). Rejected.
- **No message cap in IDB.** The IDB quota is a share of disk, so a cap isn't needed
  for space. It is kept at the operator-approved 200 because it bounds boot load and
  merge cost per session. The marker keeps rewind correct, and the cap can be raised
  later without a format change.
- **Deleting IDB from the crash page.** An IDB quota error can't reach a render
  boundary, and `deleteDatabase` blocks while any tab holds a connection. Rejected;
  clearing IDB lives in Settings only.
- **Proactive stale-slug GC** (fleet-list driven). One window's fleet list doesn't
  describe other backends on the same origin. Rejected in favour of D4 liveness,
  under pressure only.
- **An ESLint `no-restricted-properties` ban.** The console has no ESLint, and the
  rule misses `window.`/`globalThis.` forms and aliases. Replaced with a vitest
  source guard.
- **A separate origin per agent.** It breaks the single-origin hub and desktop model.
  Rejected.

## Consequences

- **A full quota can't crash the console.** At worst, history stops saving and the
  operator is told.
- **Boot is asynchronous per session.** The tab strip paints from the index, and
  transcripts show a skeleton until their record loads. Tests need an `await
  transcriptsReady()` helper.
- **Mutation API change.** Message mutators become updater-only, which touches every
  watcher that appends messages.
- **The migration is one-way.** Older consoles on the same origin see only the 24 h
  server rebuild.
- **Desktop.** The webview data directory derives from the Tauri identifier
  `studio.protolabs.protoagent` (`tauri.conf.json:5`). **Changing it loses all
  desktop history.** A Windows uninstall that removes app data also wipes it.
- **Mobile Safari.** Its tracking prevention deletes all script-writable storage,
  IDB included, after 7 days without a visit (unless the site was added to the home
  screen). IDB doesn't change that; the server rebuild covers 24 h.
- **Security.**
  - Plugin views are same-origin trusted code (ADRs 0038 and 0089). They can read
    and write every transcript, in IDB now as well, and the auth token.
    `docs/guides/plugin-views.md` states this. A namespaced helper in plugin-kit
    would be for attribution in Settings → Storage, not isolation.
  - Incognito sessions (ADR 0069 D3b scopes *server memory*) still persist in the
    browser, as they do today.
- **Dependencies.** `idb` is a runtime dependency and `fake-indexeddb` a dev one.
  Regenerate attribution (`scripts/gate.py --lint-only`).
- **Dev flag.** `storage.simulateQuotaBytes` (ADR 0068) makes the seam and IDB throw
  above N bytes, for manual QA and e2e. A test hook forces a render-time quota throw,
  so the crash page stays testable after D1.

## Delivery

Each slice ships on its own, and the crash is fixed by slice 1. Code slices carry a
`changelog.d` fragment, and only the last slice says `Fixes #N`.

1. **Seam and crash-proofing (D1, AppCrash part of D6).**
   - `lib/storage.ts` with the key registry, including `persistStorage` and
     `writeKeyStrict`.
   - The zustand adapters.
   - The strict credential writes.
   - Listener isolation in `dispatch`.
   - The quota latch.
   - The vitest source guard.
   - `AppCrash`'s in-realm flag and broadcast reset.
   - The `simulateQuotaBytes` flag.
2. **Visibility (D6).** The signal, toast, banner, client-log transitions, and
   Settings → Storage.
3. **`localStorage` limits and eviction (D3, D4, D5-localStorage).**
   - The compact v2 dedupe.
   - The 200/100 caps with a correct marker, applied to the live store.
   - The dismissal timestamps.
   - Presence-ping liveness (D4).
   - Eviction order 1–3.
4. **Load barrier (D2, part 1).** No storage change yet:
   - updater-only mutators across all callers;
   - `loadState`, with its guards in hydration, unused-session, reattach and
     reconcile;
   - PaletteChat's gate.

   These are exercised with an artificially async load.
5. **IDB store (D2, part 2).**
   - `transcriptStore.ts`, with per-tenant databases.
   - The derived index and its reconcile.
   - The `rev` transactions.
   - BroadcastChannel sync.
   - Merge-safe migration.
   - `FleetTurnWatch` switched to the index.
   - Palette/DM threads.
   - Tenant switch opening the new database.
6. **IDB eviction and polish (D5-IDB).** Eviction on quota error, the Settings IDB
   rows, and `persist()`.

## Verification

- **Unit tests, fake `Storage`** that throws above N bytes:
  - `writeKey` → evict → retry → latch;
  - registry exact matching, with the `.dismissed` key surviving;
  - eviction order and protected keys;
  - zustand `set()` and migrate-on-hydrate never throwing under a full quota;
  - the credential sites failing loudly;
  - `dispatch` isolation.
- **Unit tests, `fake-indexeddb`:**
  - migration merge, including a two-tab race, a crash between commit and v1
    removal, and an old-build tab rewriting v1;
  - the load barrier: a read that never resolves, then hydrate, then a scheduled
    card and a send, leaves the record unchanged;
  - `failed` never treated as empty;
  - `rev` conflicts and meta patches;
  - index reconcile, both orphan directions;
  - the trim marker's occurrence math against `rewind_op` fixtures, split-turn
    boundaries and cast;
  - tenant-scoped databases isolated;
  - eviction honouring presence and `liveTaskId`.
- **e2e tests:**
  - a pre-filled `localStorage` (via `simulateQuotaBytes`) boots, the v1 blob
    migrates, and a new turn survives reload;
  - two pages sync over BroadcastChannel;
  - the forced render-time quota throw recovers through **Free up space & reload**.
- **Manual QA** in the desktop app (WKWebView, WebView2): IDB persists across an app
  update, and two windows on different backends stay isolated.
- **Guard self-test:** a fixture proves the source guard fires on each banned form.
