# ADR 0135 — The always-on orchestrator: the hub as the fleet's chief of staff

- Status: Accepted (2026-11-18, Josh: "as suggested"). Phase 1 was boarded 2026-11-18. Phases 2–4 were regrouped into waves A–C the same day (see "Update" below).
- Date: 2026-11-18
- Deciders: Josh Mabry; protoAgent maintainers
- Amends: [ADR 0007](./0007-directory-aware-operator-agent.md) (the operator persona may ship as an opt-in archetype, not only as a fork).
- Builds on: [ADR 0042](./0042-fleet-supervisor-unified-console.md) (fleet supervisor), [ADR 0067](./0067-standalone-watch-primitive.md) (watches), [ADR 0079](./0079-autonomous-operating-model.md) (operating model), [ADR 0124](./0124-per-caller-run-command-approval-policy.md) (approval policy, Proposed), [ADR 0129](./0129-work-consolidation.md) (Work), [ADR 0131](./0131-work-knows-its-conversation.md) (observe-only supervision; names the health loop as out of scope).

## Context

Josh, 2026-11-18: the main-instance agent should be "the quintessential always on agent, monitoring and managing the operators needs and the fleet, making sure that things are running correctly and that any issues are escalated to the user or handled if it can." It is also the agent we demo from.

**What the hub is today** (a fleet hub on v0.249.0 with 10 local members and 3 remotes):
- Its SOUL calls it the fleet orchestrator, but it only acts when asked. It has no scheduled jobs, no watches, no directives and an empty decisions queue. Every standing job in the fleet belongs to a member.
- Its model can call `delegate_to` and `list_agents`. `fleet_diagnostics` exists and is off by default.
- Its SOUL forbids restarting a member unless asked.
- Its friction log records a delegate badge that stayed green while every dispatch timed out. A failed dispatch still does not degrade the badge.

**What the runtime ships** (main at `ecb40496c`):
- Present: cron and one-shot schedules with missed-fire recovery, watches, a priority inbox, bus-triggered turns, the decisions queue, the four-group Work view, task-stall sweeps, compaction that preserves directives.
- Absent:
  - an agent heartbeat (ADR 0007 §8 leaves it to forks);
  - a fleet health loop or alert path (ADR 0131, out of scope), and any restart of a crashed member (`fleet.autostart` runs only at hub boot);
  - proactive delivery beyond new decisions (`notify_chat_surfaces` has one caller);
  - server-side push, quiet hours, a spend cap for unattended turns;
  - instance-wide standing orders (directives are per chat);
  - a persistent home conversation (ADR 0129 D6 made `system:activity` a feed).
- Agent-created cron jobs silently expire after 7 days (`src/tools/scheduler_tools.py`).
- The scheduler already keeps an `occurrences` ledger and outcome counters, but the operator cannot see them and nothing alerts on a missed slot.
- An unattended turn auto-answers up to three approval interrupts with "no interactive operator available", then gives up. Only `request_decision` parks and waits.

**Outside norm** (OpenAI dots, xAI Grok Bot, Nous Hermes Agent, OpenClaw, Claude Duet, LangChain ambient agents):
- Table stakes: a cheap periodic check plus cron plus event triggers; a silent path when there is nothing to report; owner-only messaging with a morning brief; tiered approvals with a never-list; unattended contexts that deny instead of waiting; bounded, editable memory; visible cost.
- The interrupt taxonomy is notify / question / review, backed by an inbox because chat pings get lost.
- Documented failures: full-context heartbeats that burn hundreds of dollars a day, exposed gateways, unvetted skill marketplaces, scheduled jobs that confirm but never fire, notification overload.
- No product found detects a stuck agent, restarts it and escalates to a human. Grok Bot's Chief-of-Staff Bot coordinates work without documented health checks; enterprise guardian agents watch policy, not liveness.

## Decision

**D1. The hub is the fleet's chief of staff.** It detects, routes, escalates and proves what ran. It does not do members' work and does not take over their duties.

**D2. One unattended action.** The hub may restart a local member whose process is dead, with bounded attempts, an audit entry and a notice. It never restarts a member the operator stopped on purpose and never acts on a remote. Every other consequential action becomes a decision that shows the exact action.

**D3. Lanes stay where they are.** Code fixes and friction triage stay with the project-manager member. Infrastructure monitoring stays with the member that owns it. Members keep their own schedules; the hub never schedules on a member's behalf.

**D4. An opt-in archetype in core, not a fork.** The orchestrator ships as a catalog archetype plus generic core seams, all off by default. This amends ADR 0007, which kept the operator persona out of the template. The reason is that the demo agent must be the thing people install.

**D5. The heartbeat is cheap and quiet.** The fleet check and the morning brief are ordinary scheduled jobs. Each runs in its own small job session on the aux model and replies `NO_UPDATE` when nothing changed. No job carries the operator's full chat context.

**D6. Reaching the operator.** Proactive output is one of three kinds: notify, question (a decision) or review (approve an exact action). Delivery goes to the owner only. A chat-channel DM is the first transport; server-side push follows. Quiet hours hold low-importance notices for a digest.

**D7. Trust is built in, not added later.** Unattended turns get a spend cap, a stated allow / ask / never policy that defaults to deny (ADR 0124), and a visible run history with a missed-slot alert.

**D8. Extend the existing seam; no parallel logic and no stopgaps.** Every slice names the seam it extends, and a slice that would add a second mechanism beside an existing one is rejected at review:

| Need | The one seam |
|---|---|
| Member health and restart | the supervisor's probes, `_alive` and the autostart start path; the delegate health record reads the same result for a delegate that is also a member |
| Reading a member | `_fetch_member_json` (auth swap, timeout, failure containment) and the `fleet_diagnostics` bounds and redaction |
| Paging the agent | the inbox store and its dedup key |
| Reaching the operator | `notify_chat_surfaces` and the adapter `notify` hook; a question is `request_decision`; push is one more adapter |
| Standing work | the scheduler, and the ADR 0129 D2 follow-up record when it lands |
| Standing orders | the directive store, with an instance scope added |
| Unattended policy | ADR 0124, replacing the fixed auto-answers |
| Spend | the per-turn `turns` records in the telemetry store and the existing autonomous-origin classification (the ADR 0122 usage store is the subscription-window cache, not per-turn accounting) |
| Run history | the scheduler's `occurrences` ledger |

Nothing ships as a workaround for a later phase. In particular the hub does not poll member health from a scheduled model turn while #5533 is unbuilt: liveness is the supervisor's job, in code.

## Phases

| Phase | Scope | Tracking |
|---|---|---|
| 0 | Config only on the hub, limited to what is already the end state: SOUL with standing duties, `fleet_diagnostics` on, a morning brief | this ADR |
| 1 | Fleet health loop and crash watchdog; agent fleet tools; honest delegate health; no silent 7-day expiry | #5533, #5534, #5535, #5536 |
| 2 | Notify / question / review delivery; quiet hours and digests; server-side push | #5537, #5538, #5539 |
| 3 | Spend cap; unattended approval policy; instance-wide standing orders; run history | #5540, #5541, #5542, #5543 |
| 4 | Orchestrator archetype; home conversation; demo script | #5544, #5545, #5546 |

Phase 0 goes first because it costs nothing and shows which gaps hurt before core work is boarded. It contains no stand-in for a later phase. Console UI in phases 2–4 needs a design-system spec and preview approved before a card is cut.

## Update (2026-11-18): phases 2–4 become waves, and five decisions

Josh reviewed phases 2–4 and decided:

- **D9. Home conversation.** Amend ADR 0129 D6: results are shown in one pinned home chat while each job keeps its own session. The amendment is written with the design spec for #5545.
- **D10. Push is a requirement.** Notices reach every operator device and do not depend on a chat plugin. The console has no service worker or web manifest today, so #5539 starts with a design slice that settles the transport per device class.
- **D11. Urgency is not a model judgment.** Importance is a fixed property of the notice type, set by the code path that raises it.
- **D12. Spend cap.** At the hard limit, all unattended work on the instance pauses and one decision is raised. The cap counts tokens as well as cost.
- **D13. Standing orders need no new guards.** #5542 builds directly on the directive store. The unbuilt ADR 0129 D4 guards are not a prerequisite; a directive is still recorded only from an operator chat turn.

The phase numbers stay as issue groupings. The build order is by what each wave unblocks:

| Wave | Issues | Notes |
|---|---|---|
| A — the loop works end to end | #5537, #5539, #5540, #5543 (server side) | Server work. With phase 1 it is what a member-down notice and a morning brief need. The spend cap lands before the first standing job. |
| B — design-led | #5538, #5545, the Work rows for #5543, notice and device settings for #5537 and #5539 | Each needs a design-system spec and preview approved before its card is cut. Specs start in parallel with wave A. |
| C — policy and packaging | #5541, #5542, #5544, #5546 | #5541 and #5542 are independent server work. The archetype (#5544) is last: it packages what the other cards built. |

The hub is switched on after wave A is in a release: the health loop and restart are enabled, its SOUL gains the standing duties, and the morning brief is created.

## W2 — Push transport design (2026-11-18, proposed; Refs #5539)

This section builds on D9–D13 from PR #5563. **The transport design is proposed
for Josh's read; it does not change D10's accepted status or claim push has
shipped.** The console currently has no service worker or web manifest.

### One notice seam, every operator device

Push is **one adapter behind W1's notice seam (`notify_chat_surfaces`, card W1a,
#5537)**, not a second sender. Today that function accepts `(text, link)` and
registration is coupled to chat-plugin lifecycle. W1a must supply structured
notices and core adapter registration at hub startup without requiring a chat
plugin, chat ingress or model invocation, while preserving existing adapters.
W2 consumes that contract rather than introducing another notice producer/store.

W1 owns notice identity, notify/question/review kind, code-assigned importance
(D11), expiry, canonical target and quiet-hours/digest eligibility. W2 owns
transport attempts and fans eligible notices out to **every live, enrolled
operator device**; agent-kind pairings are excluded. Success on a chat surface
or focus on one device never suppresses delivery to another. Unsupported,
unpaired, permission-denied and unsubscribed devices must appear as not reachable
by push. D10 requires an enrollment path for every supported device class;
permission cannot be granted on the operator's behalf, and OS delivery is not
guaranteed. `apps/web/src/lib/notify.ts` stays the foreground path, including its
existing callers.

### Transport by device class

| Device | Proposed transport | Requirements and limits |
|---|---|---|
| iPhone / iPad | Standards-based Web Push to a service worker in the installed console web app | iOS/iPadOS 16.4+ support Home Screen web apps. Install at the home origin, open the installed app, pair there if its storage is separate, then enable notifications through a user gesture. An ordinary browser tab is not the supported iOS push target. |
| Android phone / tablet | The same Web Push adapter and worker; offer an installed web app | Detect Push API and Notifications support and request permission on a user gesture. Installation is the preferred phone experience, not a universal Android Push API prerequisite. Background delivery depends on browser/OS restrictions. |
| Desktop browser | The same Web Push adapter and worker | A tab need not stay open; installation is optional on supported browsers. Browser exit, disabled background operation, sleep and OS notification settings can delay or suppress presentation. Verify each browser/OS; do not promise delivery after every kind of browser shutdown. |
| Desktop app | W1 notice delivery to the running Tauri shell through its existing native notification plugin | An authenticated native notice-stream/reconnect consumer remains active in the tray, independent of webview timers, while the hub runs. Do not assume WKWebView/WebView2 supports browser Web Push. Window-close keeps the shell running today; Quit removes this receiver. If the hub runs separately after Quit, an enrolled desktop browser is the background target; the hub alone cannot wake a terminated Tauri app in this design. |

The iOS install and gesture requirements are documented by
[WebKit](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).
Use a **trusted HTTPS home origin** for remote devices, as in
[ADR 0121 D1](./0121-device-pairing-over-an-operator-supplied-public-url.md#d1--one-home-origin-per-device-the-address-set-arrives-after-the-claim).
The Push API requires a secure context; the localhost development exception does
not make an HTTP LAN address secure on a phone. See
[PushManager.subscribe](https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe).
A tailnet HTTPS origin works: providers do not connect inbound to the hub. The
hub needs outbound internet to send; the phone needs its home origin to enroll
and open details. Push can arrive without Tailscale connected, but opening the
console still requires it. An identity-aware proxy may require login on click.

Add a web manifest with stable app id, start URL, standalone display and icons,
and a same-origin worker scoped to `/app/`. Serve these as non-secret console
static assets without opening authenticated APIs or bypassing edge login. Handle
`push` and `notificationclick`; this is not an offline transcript cache and must
not cache authenticated API responses. Display generic payload text without
fetching the hub or holding its bearer token. On click, focus/open a same-origin
console window, authenticate there and resolve the notice target.

The native receiver consumes W1's notice ids with bounded replay after reconnect.
Remote shells enroll through existing device pairing. A local full-scope shell
can establish its own device row through the existing device-mint operation;
there is no native-device registry. Coordinate browser worker, native receiver
and live-console presentation by notice id per device to avoid duplicate banners.
Route W1 notices to one presentation owner per surface, preserving unrelated
`lib/notify.ts` callers. Browser push must still produce a visible notification,
using a stable tag to replace duplicates; it is not silent background sync, even
with an open tab. [Apple requires visible presentation for web pushes](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers).

### Subscriptions belong to paired-device records

Extend `security.devices.Device` and its existing `devices.json` persistence with
an optional push binding, using the existing locked/atomic update path. Key it
by **stable device id**, not the raw token or rotating token hash. Ordinary
ADR 0121 token rotation preserves it; old rows load without it. One browser
profile/installed app at one home origin has one active subscription per row;
independently paired profiles remain separate Devices entries. Native bindings
record their transport instead of a Web Push endpoint. No second device registry.

A Web Push binding stores endpoint, `p256dh` public key, `auth` secret, normalized
home origin, VAPID key version, subscription generation, optional browser
`expirationTime`, enrollment time and bounded delivery status (last attempt,
service acceptance, error category and retry time). Treat endpoints and keys as
secrets: exclude them from public device serialization, logs, diagnostics and
config exports. Devices responses expose only safe status/times. Protect storage
with owner-only permissions or equivalent Windows ACLs.

Proposed authenticated `GET/PUT/DELETE /api/devices/self/push` operations expose
safe status and the public application key, replace the caller's binding, and
disable it. Derive device id from auth, enforce `kind == device`, and allow
device-scope callers to change only their own binding. A shared bearer without
a device id must pair first. Preserve ADR 0121's credential-plane ceiling.
Require the current device credential for binding changes: a retired handoff
credential cannot overwrite the new home's subscription during its grace.
Validate home origin against the authenticated console origin and existing
offered-origin rules. Reject an endpoint already owned by another device instead
of silently transferring it. Serialize changes with token/handoff/revoke updates.

Endpoints are untrusted outbound URLs. Allow only HTTPS endpoints on explicitly
supported Apple, Google and Mozilla service hosts, using exact-host or controlled
subdomain matching, normal TLS verification, bounded input sizes, no userinfo or
redirects, and rejection of private/loopback/link-local resolved addresses. The
enrollment API must not become SSRF or an open relay; new providers need explicit
support and tests.

Successful ADR 0121 home-origin handoff retires the old binding and queued attempts;
the new origin subscribes under the same device id. An abandoned handoff offer
does not disable the old home. Browser subscriptions do not migrate across origins.
Unpairing, reuse-detection revocation and idle expiry stop sends through
`security.devices.is_live` and clear/disable bindings. Background sends and
service receipts **never refresh `last_seen_at`**. Recheck device liveness and
subscription generation before sending and before applying results; a late `410`
for an old endpoint must not delete its replacement. Already accepted pushes
cannot reliably be recalled after revoke, hence the generic payload.

### Per-instance key generation, storage and rotation

Use Web Push with VAPID (P-256), not separate APNs/FCM application credentials.
Generate one keypair from OS cryptographic randomness on first enablement and
persist a versioned keyring atomically under `instance_paths().store("push")`,
with owner-only permissions/Windows ACLs. Private keys never enter config YAML,
frontend assets, logs or Devices responses. Authenticated enrollment gets only
the active public key/version. Named instances have independent keys, not a
machine-wide or vendor shared key. Restarts reuse the keyring; corruption or loss
with existing bindings fails visibly rather than silently replacing their identity.

VAPID signs short-lived tokens with the service origin as audience, at most
24-hour validity, and an operator-configured contact URI (`mailto:` or HTTPS).
Providers see that contact; do not silently borrow an email from another setting.
Any new configuration field needs its settings-schema entry. VAPID authenticates
the sender; subscription keys separately encrypt the payload. See
[RFC 8292](https://www.rfc-editor.org/rfc/rfc8292).

Rotation is an explicit full-scope operator action. Activate a new version and
retain the old signing key for a proposed 30-day migration window. On their next
authenticated visit, clients unsubscribe/resubscribe with the new public key and
replace their binding. Existing subscriptions cannot simply be sent with a new
VAPID key. At the deadline disable unmigrated bindings as "Renew on this device"
and delete the old key. Compromise retires the key immediately, cancels pending
attempts and requires renewal of affected bindings. Protect backups as secrets;
an instance cloned into another active hub must generate new keys and re-enroll,
not share the original's subscriptions.

### What leaves the machine

The hub sends outbound HTTPS **directly to the subscription's browser-selected
service**: Apple's Web Push/APNs infrastructure for Safari/iOS Home Screen apps,
Google's FCM for Chrome, or Mozilla's push service for Firefox. There is no
protoAgent-hosted relay or chat-plugin dependency, and Apple Developer Program
membership is not required. Check provider endpoint support at implementation;
do not assume every Chromium browser uses Google's service. See
[Apple's documentation](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers),
[Google's protocol explanation](https://web.dev/articles/push-notifications-web-push-protocol)
and [Mozilla's service description](https://support.mozilla.org/en-US/kb/push-notifications-firefox).

Providers receive the endpoint, hub public IP, timing, ciphertext size,
TTL/urgency headers, VAPID public key and signed claims including the contact URI.
They also operate the device delivery connection and observe its connection
metadata. Web Push encrypts content for the subscribing browser; providers do
not receive plaintext notices. This is not metadata-private delivery. Enrollment
also connects the browser to its provider. The native Tauri path uses the hub
connection and local OS notification API, adding no Web Push provider request.

The proposed versioned payload is capped at 2 KiB before encryption: opaque notice
id, kind, fixed importance, created/expiry times, generic title ("protoAgent needs
your attention"), generic body ("Open the console to view a decision" or "Open the
console to view an update"), and a relative console route carrying the opaque id.
It includes **no** decision question/answer, review action, chat text, job output,
member/repository names, file paths, credentials, subscription secrets or absolute
private hub URL. Lock screens show generic copy. The worker accepts only known
same-origin routes, never arbitrary destinations or executable action commands.

Clicking fetches the current authoritative record after authentication; push
does not grant approval or answer from the lock screen. Answered, expired,
withdrawn and cancelled decisions get their real terminal presentation and no
answer controls. A `409` or `answered_elsewhere` transport result is not proof of
an answer: the returned record determines presentation, preserving the existing
missing-record fallback separately.

### Expiry, delivery failures and evidence

Service success means **accepted by the service**, not displayed, read or answered.
W1 keeps the notice/decision as source of truth; a failed transport never rolls
it back. Use W1's delivery queue to track `(notice_id, device_id,
subscription_generation, transport)` attempts and resume after hub restart,
without a second notice ledger. Ambiguous timeouts can cause duplicate delivery;
per-device dedup and notification tags limit it, not an exactly-once promise.

- Set provider TTL to the lesser of W1's remaining notice lifetime and 24 hours;
  drop already expired work locally. Recheck decision state before retries and
  discard obsolete prompts. Queued generic notifications may still arrive after
  state changes, so click always resolves current state.
- Known subscription expiry stops sends at that time. Missing `expirationTime`
  does not mean immortality. Reconcile `getSubscription()` and permission at each
  authenticated app start; use `pushsubscriptionchange` where supported but never
  rely on it alone. A worker without credentials defers renewal to foreground
  authentication instead of storing an operator bearer token.
- Provider `404`/`410` invalidates that subscription generation and cancels its
  attempts. Unsubscribe/permission denial disables it. Show renewal/permission
  status rather than repeatedly prompting.
- Timeouts, network failures, `429` and `5xx` retry independently per destination
  with jittered exponential backoff: at most five attempts, 30-second initial
  delay, one-hour delay cap. Honor a longer `Retry-After` if it fits the notice
  lifetime; otherwise expire. Bound request timeouts, concurrency and queue size;
  report overflow rather than claiming delivery. One endpoint cannot block others.
- `401`/`403` indicates sender-key/contact or provider-policy trouble, not proof
  of unpairing. Pause that destination/key version and expose a redacted operator
  error. Other permanent `4xx`, including oversized payloads, fail without blind
  retries until the sender is corrected.
- Revoke, idle expiry and handoff cancel pending attempts even offline. Missing
  device rows fail closed. The next authenticated visit shows undelivered notices
  and transport status; delivery errors must not recursively create unlimited
  notices about delivery errors.

TTL and service semantics follow [RFC 8030](https://www.rfc-editor.org/rfc/rfc8030).
Hub downtime prevents new sends; only previously accepted, unexpired pushes may
remain at providers. OS Focus, denied permission and offline devices remain
honest delivery limits.

### Proposed build cards — not boarded

**These are proposals only, not boarded. They need Josh's read of this design
before scheduling.** "New" paths are proposed, not shipped APIs. Each card
includes focused tests and operator documentation. Wave B settings work also
requires the design-system spec and preview approval already required above.

| Proposed title | Files / seams | Dependency |
|---|---|---|
| W2a — Bind push enrollment to paired devices | `src/security/devices.py`, `src/operator_api/pairing_routes.py`, `src/a2a_impl/auth.py`; new `src/security/push_keys.py`; `tests/test_device_pairing.py`, `tests/test_device_handoff.py`, new push enrollment/key tests; `docs/reference/operator-api.md` | Josh's design read and W1a contract agreed. Cover self-only access, token rotation, handoff/revoke races, idle expiry, endpoint validation and key migration. |
| W2b — Web Push adapter and bounded delivery | `src/graph/plugins/chat_surface.py`, new `src/graph/push_transport.py`, W1 delivery queue/lifecycle files, `src/server/` lifecycle wiring; `pyproject.toml` and `uv.lock` if a library is added; new adapter tests | W1a shipped and W2a. Fake-provider tests prove fan-out without chat plugins, bounded retries/restart recovery, partial failures, stale generations and missing devices. |
| W2c — Installable console and closed-page receiver | `apps/web/index.html`, `apps/web/public/` (new manifest, icons and worker), `apps/web/src/main.tsx`, new `apps/web/src/lib/push.ts`, `apps/web/src/lib/pairing.ts`, `apps/web/src/lib/deviceHandoff.ts`, `apps/web/src/lib/notify.ts`; console static serving in `src/server/`; focused unit tests and new `apps/web/e2e/push.spec.ts` | W2a/W2b and approved enrollment UX. Test secure origins, denied permission, renewal, click routing and duplicates. Real-device acceptance: locked paired iOS Home Screen and Android apps receive a decision and urgent notice with console closed and chat plugins disabled. |
| W2d — Native desktop receiver while the hub runs | New `apps/desktop/src-tauri/src/notices.rs`, `apps/desktop/src-tauri/src/lib.rs`, `apps/desktop/src-tauri/Cargo.toml`, `apps/web/src/lib/notify.ts`, W1 event/replay seam; focused Rust and web bridge tests | W1a/W2a; agree presentation ownership with W2c. Test tray/window-close, reconnect/replay, permission denial and Quit boundaries on macOS, Windows and Linux, without assuming WebView Push API support. |
| W2e — Device reachability, privacy and renewal controls | `apps/web/src/settings/sections.ts`, `apps/web/src/settings/devices.css`, `apps/web/src/settings/devicesSection.test.ts`, `apps/web/e2e/devices.spec.ts`; `src/graph/settings_schema.py` and config/golden files if fields are added; `docs/guides/phone-access.md`, `docs/guides/pairing.md`, `docs/explanation/security-and-trust.md` | W2a–W2d and approved Wave B spec/preview. Show per-device unsupported/denied/renewal/service-accepted states, provider disclosure, rotation effects and HTTPS/install instructions; never label service acceptance as read. |

## Consequences

- The hub gains standing work, so its cost is no longer zero when idle. D5 and the spend cap bound it.
- Until #5533 ships, the hub does not notice a member crash on its own and cannot restart one unattended. The SOUL does not claim otherwise.
- Until #5536 ships, a standing job is created by the operator (console or API), which sets no expiry. An agent-created one lapses after 7 days.
- Until #5537 ships, a proactive result reaches the operator's phone only if the job's own turn sends it with a chat-plugin tool.
- ADR 0131 D5 stays in force for board work: supervision of cards is observe-only. D2 here covers process liveness only.

## Alternatives considered

- **Config and SOUL only.** No code, but no delivery with the console closed, no restarts and no spend cap. Kept as phase 0, not as the end state.
- **A fork**, as ADR 0007 intended. Rejected: fork upkeep, and the demo agent stops being the template.
- **A first-party plugin holding all of it.** Rejected as the primary path: the health loop, delivery seam, spend cap and approval policy are host concerns. Kept as the fallback if core seams stall.
- **Hermes Agent or OpenClaw as the always-on brain.** Rejected: the external-runtime path loses goals, telemetry and the plugin surface, and OpenClaw's security record is poor.
