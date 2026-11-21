# Work view additions: spend today, run history, instance-wide orders

Status: **Approved (Josh, 2026-11-21: "approve"). Frozen; build cards via protoEngineer.**
Epic: protoLabsAI/protoAgent#5532 (ADR 0135, `docs/adr/0135-always-on-orchestrator.md`). Issues: #5540 (spend cap), #5543 (run history), #5542 (instance-wide standing orders).
Preview: artifact `a-1795144841481-360da3` (three state menus, wide and 390 side by side, theme switch).
Builds on: four-group spec `a-1794444155551-c2677f` (v9), third-pass layout spec `a-1794389295589-cf01da`, preview `a-1794444108874-db544b` (v13). Their tone map, row layout, detail view, Technical details block, inline confirm and stale states apply unless this spec says otherwise.

## 0. Open questions for Josh

**Q1. Decided (Josh, 2026-11-20): the spend cap pauses at the admission gate, not through the scheduler's `enabled` flag.**
The pause stays where the agent admits new unattended work (ADR 0135 D12). It is not the scheduler's per-job `enabled` flag. The #5540 brief's "Pause through the scheduler's existing enable flag" is superseded. In Work, a spend-cap pause shows as **one agent-wide paused state with its decision to answer** (§1), never as individual jobs switched off (§1.5). The code already works this way:

- `autonomous_admission_error` (`src/server/turn_control.py:455-486`) refuses every turn whose origin is in `_AUTONOMOUS_ORIGINS` (`turn_control.py:81-83`: scheduler, watch, inbox, webhook, background, background-resume, delegate-result) while `spend_cap_status()["paused"]` is true.
- The decision it raises says "An answer permits all unattended work for the current UTC day. Job settings are unchanged." (`turn_control.py:474-476`).
- So the pause also stops watch, inbox and webhook turns, and no job is disabled. Jobs keep their own `enabled` state.

**This spec follows the code and the decision.** The UI says "Scheduled jobs, watches, inbox and webhook turns won't start … job settings are unchanged", and it never shows jobs as disabled. No server change is needed for this.

**Q2. Most of the server work already exists. The gaps are mostly in the console.**
Each server gap below needs a protoEngineer card before the UI that depends on it ships. Until it lands, the UI hides that part.

| # | Gap | Side | Evidence | Until it lands |
|---|---|---|---|---|
| S1 | `GET /api/telemetry/spend-cap` doesn't return the soft limits | **server** | Route returns `spend_cap_status()` (`telemetry_routes.py:285-288`); that dict has hard limits only (`turn_control.py:401-404`). Soft limits exist only in settings (`settings_schema.py:1508-1515`) and are read in `_notify_spend_soft_limit` (`turn_control.py:422-423`). | No notice tick on the meters and no "Past the notice limit" line. Meters show hard limits only. |
| S2 | Run entries have no start, end or duration, and no "reported or stayed silent" | **server** | `_row_to_run` (`src/scheduler/local.py:2753-2775`) returns `scheduled_for` only; #5543 asks for "fired at, duration, outcome, reported or stayed silent". | Rows show the slot time and outcome. No duration, no "stayed silent". |
| S3 | Run history has no cursor | **server** (minor) | Route takes `limit` only (`routes.py:1110-1111`), clamped to 1–200 (`local.py:343-344`, `1101-1104`). | "Show older runs" re-fetches with a larger `limit` (50 → 200), then disappears. |
| C1 | No web client for the spend-cap route | client | No `spend-cap` call anywhere in `apps/web/src`. | — |
| C2 | No web client for `GET /api/scheduler/jobs/{id}/runs` | client | `automation.ts:121-162` has list, add, update, cancel and enabled only. | — |
| C3 | `ScheduledJob` lacks `consecutive_failures`, `last_error`, `last_ok` | client | Fields exist on `Job` (`src/scheduler/interface.py:53-58`) and reach the wire through `Job.as_dict()` (`interface.py:67-68`, `console_handlers.py:455-458`). Web type at `apps/web/src/lib/types.ts:829-841` doesn't declare them. | — |
| C4 | `DirectiveRow` lacks `instance_wide` (and `author`, `links`) | client | Server sets `instance_wide` (`src/graph/directives.py:247-249`) and serialises the whole dataclass (`directives.py:251-252`). Web type `apps/web/src/lib/api/chat.ts:107-120` doesn't have it. | — |
| C5 | No web client for `GET /api/directives` or instance-wide withdraw | client | `chat.ts` lists only a chat's directives (`chat.ts:213-214`) and revokes through the chat `DELETE` route (`chat.ts:233-240`). The chat list route returns only that chat's ledger (`chat_routes.py:2418`), so instance-wide orders never reach Work today. | — |

No server gap blocks §3 (instance-wide orders): list, edit and withdraw routes exist (`chat_routes.py:2322-2406`). §2's run list ships without S2 and S3. §1 ships without S1.

## 1. Unattended spend today (#5540)

### 1.1 Where

A summary block at the top of Work, above the four groups, on every Work surface (rail, window, phone), **only while the spend cap is enabled**. With the cap off there is no block at all (§1.4). It is not a group and has no count. The server registers the route (`server/__init__.py:1321-1323`) always, today.

- The block is **not** a Needs you row. The spend-cap decision already lands in Needs you as an ordinary decision (`turn_control.py:469-477`); the block links to it.
- Chat status rail: unchanged. The decision's Needs you chip already covers the paused case.

### 1.2 Data

`GET /api/telemetry/spend-cap` → `{day, cost_usd, total_tokens, enabled, meter_available, daily_cost_hard_limit, daily_token_hard_limit, approved_today, decision_id, paused}` (`turn_control.py:382-404`). Refresh on Work open, every 60 s while open, and on `decision.*` events. Plus `daily_cost_soft_limit` and `daily_token_soft_limit` after S1.

### 1.3 Layout

- Heading `h2` "Unattended spend today", then `.pl-chip-meta` "UTC day 2026-11-19 · resets at 00:00 UTC, in 6 h". **The day is UTC and the UI says so** everywhere a day appears.
- A neutral `Badge` with the state word: Running · Paused · Resumed for today. Status words, never colour alone.
- Two meters, **Cost** and **Tokens**, side by side at ≥ 40rem, stacked below. Each has a caption with the text value ("$6.35 of $10.00", "541,000 of 1,000,000"), the bar, and (after S1) a tick at the notice limit plus `.pl-chip-meta` "Notice at $5.00 · passed".
- A muted line: "Counts turns started by the scheduler, watches, the inbox, webhooks and background jobs. Your own chat turns don't count."
- Copy calls the soft limit the **notice limit**: the setting's own description says it only sends a notice (`settings_schema.py:1510-1511`).

### 1.4 States

**How the state is read.** The route computes `paused = enabled and (rec.pending or (not approved and (reached or store is None)))` (`turn_control.py:404`). Three things follow:

- `meter_available: false` means paused **only while today has no answer**. Once `approved_today` is true, `paused` is false even with no meter.
- `paused` can come from the cost limit, the token limit, or a decision that is still pending while neither limit is reached today. That happens when the limits were raised after the pause, or when the decision carried over from an earlier UTC day: `spend_cap_decision()` returns a pending decision even across midnight (`src/graph/decisions.py:479-487`), and today's meter starts again from zero.
- With no meter, `cost_usd` and `total_tokens` are `null` (`turn_control.py:390-392`). Every formatter accepts `null` and shows "unknown"; none may throw.

The console reads the fields in this order: `enabled` → `approved_today` → `paused` (split by `meter_available` and `decision_id`) → running.

| State | When (route fields) | Shows | Actions |
|---|---|---|---|
| Cap off | `enabled: false`, with or without a meter | **The block is not shown at all**: no heading, badge, text or meters. Nothing renders before the first reading either, so a cap-off instance never flashes the block. A later reading with `enabled: true` shows it; one with `enabled: false` removes it. | None. Turning the cap on is in Settings. |
| Under the notice limit | enabled, not paused, below both soft limits — and **every** running reading before S1 | Meters, neutral fill (accent) | — |
| Past the notice limit | enabled, not paused, ≥ a soft limit (**needs S1**) | "Past today's $5.00 notice limit. Unattended work keeps running until $10.00 or 1,000,000 tokens, then pauses." That meter's fill is warning. | — |
| Paused at a limit | `paused`, `decision_id` set, meter available | Warning `Callout`: "Unattended work is paused for the rest of today (UTC). ‹why› Scheduled jobs, watches, inbox and webhook turns won't start. Your own chat turns aren't affected, and job settings are unchanged." ‹why› comes from the reading: `cost_usd ≥ daily_cost_hard_limit` → "It reached the $10.00 daily limit."; `total_tokens ≥ daily_token_hard_limit` → "It reached the 1,000,000-token daily limit."; both → "It reached the $10.00 and 1,000,000-token daily limits."; neither (the decision is still pending after the limits were raised, or it carried over from an earlier UTC day) → "It hit a daily limit and hasn't been resumed yet." That last sentence is true in both cases; don't say "today" or "earlier today", because a carried-over decision was raised yesterday or before. The meter at its limit gets warning fill; the other stays neutral. | **Resume unattended work for today** (answers the decision with option `resume`, the label the server uses, `turn_control.py:472`) · **Open the decision** |
| Paused, no decision yet | `paused`, `decision_id: null`, meter available | Same callout and meters. "A decision to resume appears in Needs you the next time unattended work tries to start." The decision is only created at admission (`turn_control.py:469-478`). | None. No Resume: there's nothing to answer yet. |
| Resumed for today | `approved_today`, meter available | "You resumed unattended work for today. It runs with no daily limit until 00:00 UTC, then the limits apply again." Meters neutral, may pass 100 %. | — |
| Resumed, no meter | `approved_today`, `meter_available: false` | "Resumed for today. Spend can't be measured right now." Muted: "Unattended work runs with no daily limit until 00:00 UTC, then the limits apply again." No meters. | — |
| Meter unavailable | `paused`, `meter_available: false`, `decision_id` set | Warning `Callout`: "Unattended work is paused: spend can't be measured right now. The cap fails closed, so nothing unattended starts until the meter is back or you resume for today." "Spent so far today: unknown." No meters. | Resume · Open the decision |
| Meter unavailable, no decision yet | `paused`, `meter_available: false`, `decision_id: null` | Same callout and "unknown" line, plus "A decision to resume appears in Needs you the next time unattended work tries to start." | None. Resume is hidden. |
| Stale | a refresh failed after a good load | Last reading kept, warning `Callout` "Showing the 14:02 UTC reading. Couldn't refresh. **Retry**" (same pattern as `directives.tsx:296`) | Retry |
| Error | first load failed | `Alert` warning: "Couldn't load today's spend. Unattended work still follows the cap. Only this view failed to load." **Retry** | Retry |

- Tone: warning only for paused and past-notice. Never error. Never accent fill for the block (only Needs you uses accent).
- **Resume** is the decision's answer, so it uses the decision's normal answer path and its stale handling (decision already answered elsewhere → refetch, show "Resumed for today").
- The Resume button is secondary, not primary: the decision in Needs you is the one primary call to action (four-group spec §6).
- No approval time: the route doesn't return when it was answered.

### 1.5 Jobs during a pause (decided, Q1)

A spend-cap pause is **one agent-wide state**, shown once, in the spend block, with its decision to answer. It is never shown job by job.

- Rows in Coming up keep their normal look. No job row, badge, dot or toggle changes because of the pause, and no job appears switched off. A job's `enabled` flag is still its own setting, and the pause doesn't change it.
- A job's detail doesn't repeat the pause either. Its Recent runs list (§2.3) shows any slot the pause refused as `retryable` ("Not run … it will try again"), which is what the ledger records.
- The only way to end the pause is the decision: **Resume unattended work for today** in the spend block, or the same decision in Needs you. Turning a job off and on again doesn't resume it, and the UI never suggests that it does.

## 2. Per-job run history (#5543)

### 2.1 Where

In the detail of every **Time** follow-up (a scheduler job) in Coming up: inspector on the window, push on rail and phone. Below the actions, above Technical details. Watches, waits and not-started jobs don't get it.

### 2.2 Health in the row and the detail (C3)

- `consecutive_failures ≥ 1`: the row's dot becomes `warning` and the meta ends "· failed the last N runs" (N = 1 reads "· the last run failed"). Badge stays **Time**.
- Detail **Health** field: "Failed the last 3 runs. Last worked Nov 16 14:00 UTC." (from `last_ok`), or "Last worked today 14:00 UTC.", or "Hasn't run yet." Times use the job's clock (§2.3). `last_error` shows under it in muted text, truncated to 2 lines; the full text is in Technical details.
- A failing job is **not** moved to Needs you here. The server's "fails N times in a row" notice (#5543) does that through the delivery seam.

### 2.3 Recent runs list

`GET /api/scheduler/jobs/{id}/runs?limit=20` (route `routes.py:1108-1123`, backend `local.py:1080-1122`), newest first. One bordered `ul` (the Work group list style), one `li` per run, not buttons (nothing to open):

| Run (`state` / `task_state` / `ok`) | Word | Dot | Second line |
|---|---|---|---|
| `settled`, `ok: true` | Ran | success | "Completed" |
| `settled`, `ok: false` | Failed | warning | `error`, plain text |
| `running` | Running | info | "Started, still going." |
| `claimed` | Not confirmed | neutral | "It was due, but there's no record it started." (`claimed` = intent committed, the send unproven, `local.py:333-334`) |
| `unknown` | Not confirmed | neutral | "It started, but the result never came back. It may still finish." |
| `retryable` | Not run | neutral | "It didn't start. This slot is still owed, so it will try again." (admission refused or no execution owner, `local.py:2051`, `2558`) |
| `interrupted` | Skipped | neutral | "A restart interrupted it." (The ledger doesn't record whether the next slot ran, `local.py:346-348`.) |
| `interrupted_needs_operator` | Waiting on you | neutral | "A restart interrupted it. It's in Needs you." |
| `settled`, `task_state` in `{"canceled", "cancelled"}` (both spellings, as the scheduler matches them: `local.py:1271`, `1997`, `2002`) | Canceled | neutral | "It was canceled before it finished." (`ok` is `None` for this, `local.py:2757-2761`.) |
| `settled`, `ok: null`, any other `task_state` | Ran | neutral | "Outcome not recorded." |

Rows are matched top to bottom; the canceled row is checked before the generic `ok: null` row.

- **One clock per detail.** Every time in a job's detail — the **Runs** field, **Health**, each run row — uses the job's own clock: its `timezone`, or UTC when that's `null` (`types.ts:840`). Each time carries the zone name ("Today 14:00 UTC", or "Today 09:00 Europe/Berlin"). The row meta in Coming up uses the same clock. Nothing depends on `title`: the full ISO stamp is the `scheduled_for` value in the run's Technical details, which keyboard and touch can open.
- Line 1: dot, the word (semibold), `.pl-chip-meta` time of `scheduled_for` in the job's clock.
- Each run has its own collapsed **Technical details**: `state`, `task_state`, `scheduled_for`, `task_id`, `message_id`, `job_created_at`. A run from an older generation of the id (`job_created_at` ≠ the job's `created_at`) adds "From an earlier version of this job" to line 2.
- **Show older runs** (ghost) shows **only when the page came back full**: as many runs as the `limit` requested (20). It re-fetches with `limit=200` (S3). After that the button is gone; if 200 came back (the route's maximum, `local.py:344`), a muted line says "Showing the latest 200 runs."
- Failures use warning, never error (four-group spec §6).

### 2.4 Empty and unsupported states

| Response | Shows |
|---|---|
| `200`, `runs: []`, job enabled | "No runs yet. It first runs ‹next_fire, job's clock›." |
| `200`, `runs: []`, `enabled: false` | "No runs yet. It's turned off, so it won't run until you turn it on." |
| `404` | "This job is gone and left no runs." (Route: neither a job nor a run, `routes.py:1112-1114`.) The detail itself already handles a vanished job. |
| `501` | "Run history isn't available. This scheduler backend doesn't keep run history." No retry. (`routes.py:1116-1120`) |
| Network or 5xx | "Couldn't load the run history. The job itself is fine. Only this list failed to load." **Retry** |
| Refresh failed after a good load | Keep the list, stale `Callout` with Retry. |

## 3. Instance-wide standing orders in Your instructions (#5542)

### 3.1 Data (C4, C5)

- Work fetches `GET /api/directives` **with no `status`** (`chat_routes.py:2322-2335`) alongside each chat's ledger, and merges every row into the Your instructions source. The group filters to `status === "in_force"` on the client, the same way the chat ledger does today (`useDirectives.ts:44`). The route's `in_force` count is computed before any filter (`chat_routes.py:2333`), so it is right either way.
- Why not `?status=in_force`: with a status set, the route drops closed rows (`chat_routes.py:2334-2335`). After a 409 the refetched list would no longer contain the row, the open detail would unmount, and the "This was already closed…" notice (§3.4) would never show. Keeping closed rows is what lets the detail stay mounted, read-only, as the existing chat path does (`directives.tsx:189-191`, `useDirectives.ts:85`). Instance-wide ids start `dir-i` (`directives.py:84-86`), so an id never collides with a chat's.
- Key rows by id and use `instance_wide === true` from the wire, not the id prefix, to decide how a row renders. The prefix is a fallback only if the field is missing.
- Refresh on the `directive.changed` event with `instance_wide: true` (`directives.py:206-207`).

### 3.2 Rows

- Instance-wide orders sort **first** in the group, then the chat's own, each oldest first.
- Two neutral badges: **Instance-wide** then **Instruction**. Instance-wide is a word, not a colour.
- Meta starts "Applies to every chat and unattended turn · You said “…” · Nov 18". The quote and date follow the existing provenance rule (`directives.tsx:91-96`).
- **Scope:** an instance-wide order counts in every chat. It shows under "This chat" filters and in every chat's "Told it" chip count. The shared `workGroups` selector applies this once, so Work and the rail agree (four-group spec §5 count rule).
- Approval linking (§3.1 of the four-group spec) still runs on these rows. An instance-wide order is never tied to a job.

### 3.3 Detail

- Tags: info dot, **Instance-wide**, **Instruction**. No "This chat" badge.
- Fields: **Applies to** "Every chat and every unattended turn on this instance: scheduled jobs, watches, the inbox and webhooks." · **Your words** "You said “…” in “<chat title>” · <when> (your time)", for example "· Nov 18 09:40 (your time)" (or "in a chat that's since been deleted"). The zone is in the visible text. Today the stamp is the viewer's local time (`clock()`, `directives.tsx:59-63`) and only `title={localStampTitle(...)}` (`directives.tsx:226`) says so, which keyboard and touch users can't read. · **Lasts** "Until you withdraw it".
- Actions: **Edit** (scope and conditions, `PATCH /api/directives/{id}?instance_wide=true`, `chat_routes.py:2337-2361`), **Withdraw…** (danger).
- Technical details: `id`, `instance_wide`, `session_id` (labelled "recorded in"), `status`, `created_at`.

### 3.4 Withdraw (inline confirm)

The existing inline confirm (`directives.tsx:242-256`), with instance-wide copy:

- Prompt (focused on open): "Withdraw this standing order everywhere?"
- Body: "Every chat and every unattended turn stops following it from its next turn. Anything it already scheduled keeps running until you cancel it."
- Reason (optional) `Textarea`, placeholder "For example: moving the summary to the weekly digest".
- Buttons: **Withdraw everywhere** (danger) and **Keep it**. Order follows the existing frame rule (`directives.tsx:251-255`): on the phone, Keep it first, full width.
- Calls `POST /api/directives/{id}/withdraw?instance_wide=true&reason=…` (`chat_routes.py:2363-2406`).
- **200:** the row leaves the list and the count drops in one render. **One announcement only**, plus the name of the row that takes focus (§4):
  - **One callback starts it.** On a successful instance-wide withdraw, `directives.tsx` calls `ctx.onWithdrawnEverywhere()` **before** it refetches. `WorkView.tsx` defines the callback and passes it through `WorkSectionContext` (`sectionRegistry.ts:7-19`; a new optional field). It does three things in the current render: `skipCountAt.current = count - 1`, `setWithdrawCount((n) => n + 1)`, and `setWithdrawnAnnouncement("Withdrawn everywhere.")`.
  - "Withdrawn everywhere." goes through its **own** polite region (`role="status"`, `aria-atomic="true"`), the way `savedAnnouncement` works today (`WorkView.tsx:172-175`, rendered at `WorkView.tsx:347`).
  - **Repeated withdraws are announced each time.** Setting the same string twice doesn't re-render, so a second "Withdrawn everywhere." would be silent. The build keeps the region mounted and keys its **text** on the withdraw counter: `<div role="status" …><span key={withdrawCount}>{withdrawnAnnouncement}</span></div>`. Each withdraw replaces the text node inside a region that already exists, which screen readers announce. Key the span, not the region: a live region that mounts with its text already in it is often not read. Fallback, if the key doesn't announce in testing: clear the text, then set it again on the next animation frame (`requestAnimationFrame`).
  - The list region's count announcement is **suppressed for the count change the withdraw causes, and only that one.** Today `WorkView.tsx:217-224` calls ``setAnnouncement(`${label}, ${count} items.…`)`` from an effect keyed on `[label, count, needs, loading]`, through the polite region at `WorkView.tsx:346`. A plain one-shot flag isn't enough: a `loading` or `needs` change can run the effect first and use up the skip, and then the real count change announces "Your instructions, N items." after "Withdrawn everywhere." So the ref holds the **expected count**, not a flag. In the effect, after the `loading` guard and the `previousNeeds` update (which always run):
    - `count === skipCountAt.current` → this is the withdraw's count change. Clear the ref. Skip the count message. If the same run added Needs you items, set only that part: "1 new Needs you items." — a new Needs you item is never swallowed.
    - Any other run (for example a `loading` or `needs` change before the count drops) → leave the ref alone and announce as the effect does today. Only the run that sees the expected count uses up the skip.
  - The existing toast (`directives.tsx:187`) is **not** shown on this path. The chat path keeps its toast unchanged. The server also writes the withdrawal note into the origin chat if it still exists (`chat_routes.py:2392-2404`); the console doesn't add another.
- **409:** "This was already closed: <closing sentence>. Nothing changed." (existing pattern, `directives.tsx:188-193`). Any time in the closing sentence carries its zone in the visible text, for example "you withdrew it from another device today at 10:14 (your time)". This works only because §3.1 fetches closed rows too: the refetched list still has the row, now closed, so the detail (keyed by id) stays mounted, read-only, and the notice shows in place with focus on it.
- **404:** toast "This order is no longer in the list. The list has been refreshed."
- **Network or 5xx:** warning `Alert` "Couldn't withdraw it. It's still in force everywhere." **Try again**.
- **Stale list:** the existing stale `Callout` (`directives.tsx:296`).

## 4. Accessibility

**Semantics**

- Spend block: `section` labelled by its `h2`. Run list: `h3` "Recent runs" labels its `ul`. Group headings stay `h2` with the count in the accessible name.
- Each meter is `role="meter"` with `aria-labelledby` (its "Cost" or "Tokens" caption), `aria-valuemin`, `aria-valuemax`, `aria-valuenow` and `aria-valuetext`. The same value is visible as text in the caption. The text depends on whether S1 has shipped:

| | Cost meter `aria-valuetext` | Tokens meter `aria-valuetext` |
|---|---|---|
| **Before S1** (ships first; no soft limits on the wire) | "$2.10 of $10.00 daily limit" | "184,000 of 1,000,000 tokens daily limit" |
| **After S1**, below the notice limit | "$2.10 of $10.00 daily limit, notice at $5.00" | "184,000 of 1,000,000 tokens daily limit, notice at 600,000" |
| **After S1**, past the notice limit | "$6.35 of $10.00 daily limit, past the notice limit of $5.00" | "541,000 of 1,000,000 tokens daily limit, notice at 600,000" |

- Before S1 there is no notice tick and no "Notice at …" line, so the text never mentions a notice. The console decides by the presence of `daily_cost_soft_limit` / `daily_token_soft_limit` on the response, not by a version check. `aria-valuenow` is clamped to the max; the text carries the real value when it passes 100 % (Resumed for today). **Not colour alone**: the state is also in the badge word and the callout.
- The DS `Progress` renders `role="progressbar"` with no name and no value text (`packages/ui/src/data.tsx:189-194`). Spend isn't progress toward a goal, so the console needs a meter. See DS gap G4.
- Run status is a word ("Failed") next to the dot. Dots are `aria-hidden`.
- The confirm is `role="group"` labelled by its prompt.

**Keyboard and focus order**

- Spend block: Resume → Open the decision → (Work's group list). The meters aren't focusable.
- Run detail: Back (push only) → Edit → Run now → Cancel → each run's Technical details summary → Show older runs → Technical details.
- Withdraw: opening the confirm moves focus to the prompt. Escape or **Keep it** returns focus to **Withdraw…**. **Today's code doesn't do this:** `cancel()` focuses `actionRef.current?.querySelector("button")`, the first button, which is **Edit** (`directives.tsx:176-179`). The build fixes it (§5, part 3): Withdraw… gets `data-withdraw`, and focus moves to `[data-withdraw]` after the actions re-render, since the actions div isn't mounted while the confirm shows. The effect must not fire on first open: `confirm` starts `false`, so an effect keyed on `confirm === false` alone would steal focus from the detail heading every time the detail mounts. Use a `wasConfirming` ref:
  - The layout effect focuses `[data-withdraw]` **only on a true → false transition** of `confirm` (`wasConfirming.current === true && !confirm`), then stores the current value in the ref.
  - It does **not** run after a successful withdraw. That path closes the detail and uses the row-focus rule below; `withdraw()` clears `wasConfirming` (or sets a "withdrawn" flag) before `setConfirm(false)`, so the effect skips it.
- **Focus return after withdraw:** the detail closes. Focus goes to the next row in Your instructions; if there is none, the previous row; if the group is now empty, the group's `h2` (`tabindex="-1"`). Never to `body`.
- **After Resume succeeds**, the Resume button disappears, so focus can't stay on it. Focus moves to the spend block's `h2` (`tabindex="-1"`), whose section now shows the badge "Resumed for today". If Resume fails, focus stays on the button and the decision's normal error shows.

**Live regions (polite, only where the value changes)**

- One polite region for the spend block, which speaks only when the **state** changes (Running → Paused, Paused → Resumed). It never speaks on a meter refresh.
- **Not when the change came from this block's own Resume.** Focus already lands on the heading and the badge says "Resumed for today", so the live region stays silent for that one transition. A resume answered elsewhere (Needs you, another device) is announced as usual: "Unattended work resumed for today."
- "Withdrawn everywhere." once after each instance-wide withdraw, **instead of** the toast (§3.4). It has its **own** polite region (`role="status"`, `aria-atomic="true"`), like `savedAnnouncement` (`WorkView.tsx:172-175`, `347`); its text is keyed on a withdraw counter so a second withdraw is announced again. The list region's "<group>, N items." message (`WorkView.tsx:217-224`, region at `346`) is skipped for the one effect run whose `count` equals `skipCountAt.current` (set by `ctx.onWithdrawnEverywhere()`), so the reader hears "Withdrawn everywhere." and then the name of the row that takes focus. A Needs you item that arrives in the same refetch is still announced.
- The run list has no live region; it changes only when you open it or press Show older runs.

**Contrast (checked with `theme_contrast`)**

| Pair | Dark | Light |
|---|---|---|
| `fg-muted` text on `bg-raised` | 7.24 | 7.73 |
| `fg-muted` on `bg-subtle` (current row) | 6.62 | 6.65 |
| `status-error` text (Withdraw everywhere, `.pl-btn--danger`, 13 px) on `bg-raised` (the confirm, as `.work-detail__confirm`, `work-detail.css:54`) | 5.12 | 4.74 |
| Same button on hover: `bg` text on `status-error` fill (`.pl-btn--danger:hover`, `primitives.css:358-362`) | 5.46 | **4.42** ✗ (4.5:1 text, 13 px) |
| `fg-muted` on `bg-inset` | 7.90 | 6.77 |
| `status-warning` fill on `bg-inset` track | 9.96 | **2.89** ✗ (3:1 non-text) |
| `status-info` dot on `bg-subtle` | 6.04 | 3.63 |

- The light warning fill fails 3:1 against the track (WCAG 1.4.11). Because the caption text carries the value and the badge carries the state, the bar isn't the only cue, so this is not blocking. It is DS gap G5.
- The danger button's hover state fails 4.5:1 in light (WCAG 1.4.3). It comes from the DS class, not this view, so the build uses `.pl-btn--danger` as is and doesn't block on it. It is DS gap G6.
- Meta text uses `.pl-chip-meta`, never `.pl-message__ts` (it fails contrast).
- At 390 px and 400 % zoom nothing scrolls sideways; long errors and quotes wrap (`overflow-wrap:anywhere`).

## 5. Code scope (protoAgent, for later, not boarded)

Server (protoEngineer cards, before the UI part that needs them): **S1** soft limits in the spend-cap route · **S2** run start/end and "reported" on each run · **S3** (optional) a run-history cursor.

Console, ≤ 4 files per card:

1. **Spend block:** `lib/api/telemetry.ts` (C1), new `work/SpendToday.tsx` + css, mounted in `WorkView.tsx`.
2. **Run history:** `lib/types.ts` (C3), `lib/api/automation.ts` (C2), `work/sections/schedules.tsx` (health meta and the runs list), new `work/JobRuns.tsx`.
3. **Instance-wide orders:** `lib/api/chat.ts` (C4, C5; `GET /api/directives` with no `status`), `work/useWorkItems.ts` (merge all rows; filter in force on the client), `work/sections/directives.tsx` (badge, copy, confirm, withdraw route; **the `cancel()` focus fix**: `data-withdraw` on Withdraw…, and a layout effect with a `wasConfirming` ref that focuses `[data-withdraw]` only on a true → false transition of `confirm`, never on first open and never after a successful withdraw, which uses the row-focus rule instead; on the instance-wide 200 path, call `ctx.onWithdrawnEverywhere()` before the refetch and show no toast), `work/workGroups.ts` (count in every chat). `WorkView.tsx` defines `onWithdrawnEverywhere` and passes it in `WorkSectionContext` (`sectionRegistry.ts`, new optional field). The callback sets `skipCountAt.current = count - 1`, bumps `withdrawCount` and sets the withdraw text. `WorkView.tsx` also renders the dedicated withdraw region (text keyed on `withdrawCount`), and its count effect skips only the run where `count === skipCountAt.current`, then clears the ref; other runs leave it alone. That is a fifth file, so it rides in card 1 (which already touches `WorkView.tsx`) or in its own small card.

Each card's PR attaches wide and 390 screenshots in light and dark, and an e2e asserts:

- no sideways scroll at 390;
- focus return after withdraw (next row, previous row, or the group `h2`);
- opening an instance-wide detail leaves focus on its heading, not on Withdraw… (the `wasConfirming` guard);
- Keep it and Escape return focus to Withdraw…;
- **one announcement after an instance-wide withdraw:** the withdraw region's text is exactly "Withdrawn everywhere.", the list region's text does **not** change to "Your instructions, N items." after it, and no toast is shown;
- **withdraw while a Needs you item arrives in the same refetch:** the withdraw region says "Withdrawn everywhere.", the list region announces the new Needs you item ("1 new Needs you items.") but not "Your instructions, N items.", and the next unrelated count change announces as usual;
- **two withdraws in a row:** "Withdrawn everywhere." is announced both times (the text node is replaced, keyed on `withdrawCount`).

## 6. Design-system findings (protoContent; to file once approved, after a dedupe search)

- **G4 — `Progress` can't be a meter.** It hardcodes `role="progressbar"`, has no `aria-labelledby` to its label and no `aria-valuetext` (`data.tsx:189-194`), and has no marker for a second threshold. Proposal: `Progress` gets `role="meter"` option (or a `Meter` component), label wiring, a `valueText` prop and an optional `marks` prop. The preview hand-builds this from `.pl-progress*` classes.
- **G5 — the light warning fill fails 3:1 on the track.** `--pl-color-status-warning` (light) on `--pl-color-bg-inset` is 2.89:1. Also `.pl-progress__caption` hardcodes `font-size: 12px` where `--pl-font-size-xs` exists (`packages/ui-css/styles/data.css:231`).
- **G6 — `.pl-btn--danger:hover` fails text contrast in light.** The hover rule sets `color: var(--pl-color-bg)` on `background: var(--pl-color-status-error)` (`packages/ui-css/styles/primitives.css:358-362`). That is 4.42:1 in light, which fails WCAG 1.4.3 at the button's 13 px; dark is 5.46:1. protoContent issue filed: [protoLabsAI/protoContent#687](https://github.com/protoLabsAI/protoContent/issues/687). Not blocking for this build: the view uses the class unchanged and picks up the fix when it ships.
- Existing G1 (`Empty align="start"`) and G3 (list group primitive) still apply. The preview's run list and rows are hand-built for the same reason as before. `ds_check` on the preview markup flagged only the hand-built row `<button>` (info), which is the WorkRow pattern G3 covers.
