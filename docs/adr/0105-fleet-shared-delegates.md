# 0105 — Fleet-shared delegates: one roster on the box, every member's bench

Status: **Accepted** (follow-up to #2977; amends ADR 0025 and ADR 0047)

## Context

A delegate (ADR 0025) is a per-instance record: the `delegates:` list in an
agent's `langgraph-config.yaml` plus its secrets in that agent's `secrets.yaml`.
That was right for a single agent. With a fleet (ADR 0042) it meant the coder a
user registered on the hub was invisible to every member — the 2026-08-22
fresh-setup audit found a Project Manager member whose Configure step let the
user *pick* the hub's `claude-code` and then failed its first dispatch with
"delegate not found".

#2977 closed the first-run gap by **copying** the picked entry (and its
secrets) into the member at create time. A copy is a snapshot: rotate the key
or move the binary on the hub and every member keeps the stale entry; register
a second coder later and no existing member sees it. The settings cascade
(ADR 0047) cannot carry the list either — its Host layer is a per-`Field`
overlay for scalar settings, and `delegates:` is a raw top-level list the
plugin reads straight from the YAML.

## Decision

**A delegate has a scope.** `scope: agent` (the default) is the existing
per-instance record. `scope: host` is **fleet-shared**: the entry lives in the
box's `host-config.yaml` under a top-level `delegates:` list, and its secrets
in a new owner-only `host-secrets.yaml` beside it (`delegate_secrets`, same key
shape as the instance overlay).

**Every instance under the box reads both layers.** The effective roster is
`agent ∪ host`, an agent entry shadowing a host entry of the same name —
the same precedence the settings cascade uses. Secrets merge the same way.
Nothing is copied: a member resolves a shared coder from the box file at each
load, so a change on the hub reaches every member on its next config reload.

**Only the hub writes the host layer.** A fleet member never writes box state
(the rule `sync_host_model_layer` already follows). This is an *advisory fence*,
not a security boundary: members run as the same OS user in the same trust
domain (ADR 0089) and can read `host-secrets.yaml` by design; the guard keeps
the roster's single writer obvious, it does not defend against a member. A
member's attempt to
create, edit, or delete a shared entry is refused (`DelegateScopeError`, HTTP
403), while it may still register its own agent-scoped entry of the same name
to shadow the shared one. The console's Delegates panel offers a **Share with
fleet** switch on the hub and renders shared rows read-only (a `fleet` badge)
on members; `GET /api/delegates` carries `scope` per row and `can_share` for
the instance.

**The create-time copy stays for agent-scoped picks.** `copy_host_delegates`
(#2977) skips a picked name that is fleet-shared — it is already reachable —
and still copies an agent-scoped one, so a hub that never shares anything
keeps today's behaviour.

## Consequences

- A coder registered once with *Share with fleet* on is on every member's bench,
  including members created before it existed; a rotated key propagates.
- `host-config.yaml` gains one raw section the cascade does not interpret
  (`graph/config.py` still filters the Host layer to host-scoped fields; the
  delegates store reads the file directly). `host-secrets.yaml` is a second
  box-level secrets file — 0600, atomic writes, never read by the cascade.
- A member that shadows a shared entry owns the shadow; deleting it reveals the
  shared entry again.
- The `PROTOAGENT_HOST_CONFIG` override (read-only sidecar setups) applies to
  the host secrets path too (it is derived from the host config path).
- The `dev` sandbox instance (`PROTOAGENT_INSTANCE=dev`) shares the box, so a
  delegate shared from it lands on prod and every member — by the box tier's
  design (ADR 0047); share from the instance you mean.
- An unparseable `host-config.yaml` refuses a shared-delegate save rather than
  being overwritten; a read-only host config (`PROTOAGENT_HOST_CONFIG` on a
  sidecar mount) refuses with a clear message (HTTP 403), not a 500.
- Out of scope: per-member *selection* of which shared delegates to expose
  (every member sees all of them) and remote members (ADR 0042 §I), which run
  their own box.
