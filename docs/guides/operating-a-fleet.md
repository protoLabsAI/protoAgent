# Operating a fleet

> **Read-only surface.** The fleet telemetry rollup ([ADR 0006](../adr/0006-observability-and-the-self-improving-flywheel.md)) is a **read-only observability window** — it reports what the fleet is doing, not what to do about it. Every corrective step in this guide is an explicit operator procedure that requires your approval before acting and a verification step before it is considered done. The hub never autonomously restarts or reconfigures a member.

This guide assumes your fleet is running (see [Run a fleet](./fleet.md)) and your hub is on v0.132 or later (the fleet telemetry rollup ships with ADR 0006 Slices 1–2).

---

## 1. Health-check pass

The fleet health read gives you the live state of every member in one response.

### Run the read

```bash
curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet | jq .
```

Or open **Settings ▸ Telemetry** in the console — the Fleet section renders the same rollup.

### Interpret the top-level fields

| Field | What it means |
|---|---|
| `enabled` | Whether this hub's own telemetry store is on |
| `fleet` | `true` when the roster has at least one peer member; `false` on a single-box install |
| `members` | Map keyed by member slug; always includes `"host"` for the hub itself |
| `langfuse_trace_url_template` | URL template for resolving Langfuse trace links; `null` when Langfuse is off |

### Interpret each member entry

Every entry under `members` has these fields:

| Field | Values | Meaning |
|---|---|---|
| `name` | string | Member's internal name |
| `label` | string | Display label (e.g. `"main"` for the host, `"protoEngineer"` for a peer) |
| `host` | bool | `true` only for the hub itself |
| `remote` | bool | `true` for members on a remote host |
| `running` | bool | Roster's last-known running state |
| `reachable` | bool | `false` if the fan-out GET to this member failed or timed out |
| `telemetry_enabled` | bool | Whether this member has its telemetry store on |
| `rollup` | object or null | `turns`, `cost_usd`, `success_rate`, `cache_hit_ratio` — `null` when telemetry is off |
| `flags` | array | Advise-only flagged problems from this member (see §3) |

### Reachability states

| `running` | `reachable` | Interpretation |
|---|---|---|
| `true` | `true` | Member is up and responding |
| `true` | `false` | Member is listed as running but did not respond — possible crash or network issue |
| `false` | `false` | Member is stopped — expected |
| `false` | `true` | Rare: member responded despite being listed as not running |

An **unreachable member is reported, never restarted** — the rollup always completes with a `reachable: false` entry for that member.

### Version-skew flag

When a member's app version (read from its A2A agent card) differs from the hub's, the console shows a warning badge on that member. Clear it by upgrading the lagging side.

---

## 2. Upgrade + rollout/rollback

Every mutating step below is an explicit operator procedure. Do not proceed to the next step until you have verified the current one succeeded.

### Pre-upgrade health snapshot

Before upgrading any member, capture the baseline so you have a before/after comparison:

**Approval required before proceeding.**

```bash
# Capture the current fleet health state
curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet | jq '{
    members: (.members | to_entries | map({
      key: .key,
      value: {reachable: .value.reachable, running: .value.running,
              turns: .value.rollup.turns, success_rate: .value.rollup.success_rate}
    }) | from_entries)
  }' > /tmp/fleet-before.json
cat /tmp/fleet-before.json
```

**Verify it worked:** All members you expect to be running show `reachable: true`.

### Upgrade a member

**Approval required before proceeding.**

Upgrade the target member using your deployment method (Docker pull + restart, package update, etc.). The hub's reverse proxy will continue serving other members while this one restarts.

**Verify it worked:** After the member restarts, poll until `reachable` returns to `true`:

```bash
# Poll until the member is reachable again (adjust slug as needed)
SLUG="protoEngineer-ba4c"
until curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet \
  | jq -e ".members[\"$SLUG\"].reachable == true" > /dev/null; do
  echo "Waiting for $SLUG to become reachable…"; sleep 5
done
echo "$SLUG is reachable"
```

Then diff against your baseline:

```bash
curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet | jq '{
    members: (.members | to_entries | map({
      key: .key,
      value: {reachable: .value.reachable, running: .value.running,
              success_rate: .value.rollup.success_rate}
    }) | from_entries)
  }' > /tmp/fleet-after.json
diff /tmp/fleet-before.json /tmp/fleet-after.json
```

**Accept the upgrade** only when `reachable: true`, `success_rate` is at or above baseline, and the version-skew badge is gone.

### Roll back a member

If the upgrade produces a degraded `success_rate`, new flags, or a member that stays unreachable, roll back by reverting to the previous image/package.

**Approval required before proceeding.**

**Verify it worked:** Same poll loop as above. Confirm that:
1. The member returns to `reachable: true`.
2. The `flags` array returns to its pre-upgrade length or lower.
3. `success_rate` is back at or above baseline.

### Staged rollout across the fleet

Roll out one member at a time. Run the full upgrade + verify cycle for each member before moving to the next. Do not upgrade the hub last — upgrade it first when its version is lagging, then proceed member by member.

---

## 3. Incident triage

When a member raises flagged problems, the read-only evidence in the rollup tells you exactly which turn to investigate, before you touch anything.

### Locate the flags

```bash
curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet \
  | jq '[.members | to_entries[] | .value.flags[] | {member: .member, reasons: .reasons, evidence: .evidence}]'
```

In the console, open **Settings ▸ Telemetry ▸ Flagged problems**. Each row shows the member label, flag reasons, and evidence fields.

### Read the evidence fields

Each flag entry in `flags` has an `evidence` object:

| Evidence field | What it tells you |
|---|---|
| `member` | The slug of the member that raised this flag |
| `turn` | The full per-turn row from that member's telemetry store — contains cost, latency, tool calls, model |
| `trace_id` | The Langfuse trace ID for this turn; `null` when Langfuse is off |
| `trace_url` | Resolved Langfuse URL — open it to see the full turn in Langfuse; `null` when unavailable |
| `timestamp` | ISO 8601 end-time of the flagged turn (`ended_at`) |

### Read-only first pass

Before taking any action:

1. **Note the `member` slug** — this tells you which member to look at.
2. **Open `trace_url`** in Langfuse (if present) to see the turn's full tool-call trace.
3. **Read `turn`** — look at `cost_usd`, `latency_ms`, and `success` to understand the failure mode.
4. **Read `reasons`** — these are the advise-only flag labels (e.g. high latency, high cost, failed turn).
5. **Check `timestamp`** — correlate with any deploy, config change, or external event at that time.

Only after this read-only pass do you decide whether an operator procedure is needed.

### Correlate across members

To check whether a problem is isolated to one member or fleet-wide:

```bash
curl -s -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://localhost:7870/api/telemetry/fleet \
  | jq '.members | to_entries | map({
      member: .key,
      flags: (.value.flags | length),
      success_rate: .value.rollup.success_rate,
      reachable: .value.reachable
    })'
```

A single member with elevated flags and degraded `success_rate` points to a member-specific issue. Fleet-wide degradation across all members suggests a shared dependency (gateway, LLM endpoint, knowledge store).

---

## 4. Recovery planning

Recovery procedures are explicit operator actions. Each one below names the approval gate and the verification step that closes it.

### Member is unreachable after restart

**Decision table:**

| Observation | Next step |
|---|---|
| `running: false`, `reachable: false` | Member is stopped — start it with your deployment method |
| `running: true`, `reachable: false` | Member process may have crashed — check its logs, then restart |
| `running: true`, `reachable: false` after restart | Network issue — check the member's bind address and the hub's proxy config ([ADR 0042](../adr/0042-fleet-supervisor-unified-console.md)) |
| Version-skew badge present | Upgrade the lagging member (§2) |

**Runbook — unreachable member:**

1. **Read-only first:** Capture current fleet state (see §1).
2. **Approval required:** Decide to restart the member.
3. **Act:** Restart the member using your deployment method.
4. **Verify it worked:** Poll until `reachable: true` (poll loop in §2). If it stays unreachable after 60 s, check the member's process logs before retrying.
5. **Re-read the fleet:** Confirm `success_rate` and `flags` are at or below pre-incident levels.

### Elevated flags on one member

1. **Read-only first:** Fetch the flag evidence (§3). Note `trace_url`, `turn` fields, `reasons`, and `timestamp`.
2. **Approval required:** Decide on the remediation (config change, model swap, rollback).
3. **Act:** Apply the change to the member only.
4. **Verify it worked:** Re-read the fleet rollup after the next turn completes. Confirm that `flags` length has decreased and `success_rate` has recovered.

### Fleet-wide degradation

If all members show elevated flags or degraded `success_rate`:

1. **Read-only first:** Compare `rollup.success_rate` across all members. Check whether the Langfuse trace URLs (from `evidence.trace_url`) point to a common failure mode.
2. **Approval required:** Confirm the shared dependency is the cause (gateway, embedding endpoint, knowledge store).
3. **Act:** Remediate the shared dependency — do not touch individual members until the shared root cause is resolved.
4. **Verify it worked:** Re-read the fleet rollup. All members should show recovery in `success_rate` before you close the incident.

### No operator persona

The operator persona (how the hub decides to act on this information) is a fork concern per [ADR 0007](../adr/0007-directory-aware-operator-agent.md) and is not packaged in this template. The guide above describes the read-only evidence surface and the explicit procedures an operator runs against it.

---

## See also

- ADRs: [0006 observability & the self-improving flywheel](../adr/0006-observability-and-the-self-improving-flywheel.md) ·
  [0042 fleet supervisor & unified console](../adr/0042-fleet-supervisor-unified-console.md) ·
  [0072 fleet seed / team-via-config](../adr/0072-fleet-seed-team-via-config.md) ·
  [0089 intra-instance trust boundary](../adr/0089-intra-instance-trust-boundary.md)
- Guides: [Run a fleet](./fleet.md) · [Wire Langfuse + Prometheus](./observability.md) ·
  [Delegates](./delegates.md) · [Multi-instance scoping](./multi-instance.md)
