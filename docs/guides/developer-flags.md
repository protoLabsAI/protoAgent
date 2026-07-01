# Developer flags

A **developer flag** is a temporary gate on a **pre-release** code path — a way to merge a
half-built feature to `main` (exercised internally, invisible in production) instead of parking
it on a long-lived branch that drifts. Flags are **local and static** (no A/B, no percentage
rollouts, no remote service) and they're meant to be **deleted** when the feature ships (ADR
0068).

A flag is *not* a [plugin](/guides/plugins) (a permanent capability you install/enable) and *not*
a [setting](/guides/customize-and-deploy) (permanent user configuration). It's a developer's
switch on unfinished work, with a built-in expiry.

## The model: tiers × channel

Each flag declares a **tier** — its rollout stage — and enablement is that tier measured against
the runtime **channel**:

| tier | who sees it |
|------|-------------|
| `off` | nobody (a kill switch) |
| `dev` | developers only |
| `beta` | opt-in preview users |
| `on` | everybody (shipped) |

The **channel** is `prod ⊂ beta ⊂ dev` (dev sees the most) and is *derived*, not per-flag:

- the **dev sandbox instance** (`PROTOAGENT_INSTANCE=dev`, see [multi-instance](/guides/multi-instance)) is `dev`;
- a Vite dev build (`import.meta.env.DEV`) is `dev` in the console;
- otherwise the **`developer.channel`** setting (`prod` | `beta` | `dev`, default `prod`) sets it.

So a developer on the dev instance auto-sees `dev`-tier features; production sees only `on`.

## 1. Define the flag

Add one entry to the registry — the single source of truth:

```python
# runtime/flags.py
FLAGS: list[Flag] = [
    Flag(
        id="chat.new_dashboard",       # dotted, stable — the lookup + override key
        description="Redesigned dashboard (WIP).",
        tier="dev",                    # off → dev → beta → on
        owner="you@example.com",       # who to ask
        remove_by="2026-09-01",        # ISO date (or a version) — the cleanup deadline
    ),
]
```

## 2. Gate a backend path

```python
from runtime.flags import flag_enabled

if flag_enabled("chat.new_dashboard"):
    ...  # the new path
else:
    ...  # the shipped path
```

`flag_enabled` resolves **`PROTOAGENT_FLAG_<ID>` env override → tier-vs-channel → off** (an
unregistered id is off — fail-closed).

## 3. Gate console UI

```tsx
import { useFlag } from "../flags/flags";

function Panel() {
  if (!useFlag("chat.new_dashboard")) return <OldDashboard />;
  return <NewDashboard />;
}
```

`useFlag` layers two frontend overrides on top of the server's channel resolution:
**`?flag:<id>=on|off` query param → device-local panel toggle → server state**. It's fail-closed
while `/api/flags` loads.

## 4. Flip it while you work

Without editing config or restarting:

- **Env** — `PROTOAGENT_FLAG_CHAT_NEW_DASHBOARD=on` (headless / CI / a deploy).
- **Query param** — append `?flag:chat.new_dashboard=on` to the console URL (a shareable "try
  this build" link; lasts the page load).
- **The Developer panel** — **Settings ▸ Developer** (shown off prod, or via `?dev`) lists every
  flag with its tier + state and a per-device toggle. Overrides are device-local — they never
  touch shared config. *Reset* returns a flag to its channel default.

## 5. Graduate and delete

When the feature ships, flip the tier to `on`, then — in **one PR** — **delete the flag entry and
the old code path**. A flag is a loan against future cleanup: `runtime/flags.py` is the loan book,
and `remove_by` is the due date. `tests/test_flags.py::test_no_flag_is_past_its_remove_by` **fails
CI** once a flag's ISO-date `remove_by` has passed, so a stale gate is visible debt rather than
silent accretion.

## See also

- [ADR 0068](/adr/0068-developer-flags-and-panel) — the design and the non-goals.
- [Multi-instance](/guides/multi-instance) — the dev sandbox instance that defaults to the `dev` channel.
