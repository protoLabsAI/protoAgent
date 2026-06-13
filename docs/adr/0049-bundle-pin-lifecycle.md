# 0049 — Bundle pin lifecycle (a pin means "last verified working")

- Status: Accepted
- Date: 2026-06-12
- Builds on: ADR 0040 (plugin bundles — reference manifest over standalone plugin repos),
  ADR 0027 (git-installable plugins + `plugins.lock`).

## Context

ADR 0040 made a bundle a *curated, pinned* set of plugin repos. The pin is the point: a
bundle's promise is "this combo was tested together". But the first real bundle (pm-stack,
the "Project Manager" archetype) shipped pins that were **already stale at authoring time**
— both members had landed console-view fixes (`projectBoard-plugin#2`,
`agent-browser-plugin#7`) that the pinned SHAs predated, so every agent spawned from the
archetype got 404s on its Board and Browser panels out of the box.

Three structural gaps made that possible, and would make it recur:

1. **The pin has no defined meaning.** Nothing distinguishes "verified working" from
   "where the curator happened to be standing". No process re-verifies a bundle's pin set
   against the plugins' repos or against the core version it runs on.
2. **Staleness is invisible at runtime.** `check_updates` deliberately skips the network
   for SHA-pinned entries (`pinned-skips-net`, ADR/PR #887) — correct as an auto-update
   guard, but it means a SHA-pinned bundle member *never even reports* `behind`.
   Worse, tag-pinned entries reported a **permanent false positive**: `git ls-remote <url> <tag>`
   returns the *tag object* SHA for an annotated tag (not the peeled commit), which
   never equals the lock's `resolved_sha`.
3. **Fixing the bundle repo fixes nothing deployed.** Pins are copied into each agent's
   `plugins.lock` at install; a bumped bundle repo does not propagate to already-spawned
   agents (the live incident still needed a hands-on member re-install + restart).

The question: do bundles *float* with their plugins, or *pin where it last worked*?

## Decision

**Pins stay exact — and acquire a lifecycle that makes "last verified working" literally
true.** A floating ref would let any plugin's HEAD break every archetype spawn overnight,
which is strictly worse than a stale-but-functional pin. Instead:

1. **Pin release tags, not raw SHAs.** A bundle's `ref:` SHOULD be a semver release tag
   (`v0.1.1`), giving three things a 40-hex SHA can't: legibility ("pinned v0.1.0, v0.1.2
   available"), an `ls-remote`-able ref for the runtime advisory, and a release discipline
   nudge on plugin repos. The installer resolves and locks the commit SHA exactly as
   before — the tag is the *requested* ref, the lock stays the reproducibility truth.
   (This ADR fixes `_ls_remote_sha` to peel annotated tags — `ls-remote` is asked for both
   `<ref>` and `<ref>^{}` and the peeled line wins — so tag pins compare commit-to-commit
   and the false "behind" disappears.)
2. **The bundle records what it was verified against.** A `verified_against:` field
   (core version, e.g. `0.35.3`) documents the half of the compat contract that
   `min_protoagent_version` (plugin → core) doesn't cover: *bundle pin-set → core*. It is
   metadata, not a gate — surfaced by tooling, never enforced at install.
3. **A verify-and-bump loop owns the pin.** The bundle repo carries CI that (on PR,
   on dispatch, and on a schedule) installs the manifest's pin set into a scratch agent,
   enables every member, and probes **every declared console-view path** for 200 — the
   exact check that would have caught the pm-stack incident at authoring time. The
   scheduled leg also ls-remotes each member for a newer release tag and opens a pin-bump
   PR, which must pass the same verify to merge. After this, the pin only ever moves
   through a passing verification: "pin where it last worked" stops being an intention
   and becomes the mechanism. A reference implementation ships in-repo under
   [`examples/bundles/template/`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/bundles/template).
4. **Runtime surfacing (kept, and now honest).** Tag-pinned members keep reporting
   `behind` via the (fixed) ls-remote compare. SHA-pinned members keep `pinned-skips-net`;
   the `update_available_but_pinned` advisory and the bundle-level re-pin endpoint that
   *propagates* a bumped bundle to already-spawned agents remain on the version-coherence
   P1 queue (`docs/dev/version-coherence.md`) — this ADR defines the contract they
   implement, not the implementation.

## Consequences

- **A bundle pin is a claim with a test behind it.** The verify loop turns curation from a
  one-time act into a maintained property; a red scheduled run is the "your bundle rotted"
  alarm that did not exist before.
- **Plugin repos are nudged toward tagging releases.** Raw-SHA pins still work (and remain
  right for unreleased forks), but the template, docs, and pm-stack all model tags.
- **The annotated-tag fix un-breaks the existing UI.** Tag-pinned plugins (e.g.
  `artifact@v0.2.1`) showed a perpetual "Update available" that re-installing could never
  clear — update-to-same-SHA, still "behind". That loop is gone.
- **Deployed agents still need propagation.** This ADR deliberately does not auto-update
  spawned agents when a bundle bumps; that is the P1 re-pin endpoint's job, gated on the
  operator. Until it lands, a bundle bump reaches only new installs.
- Minor: the verify loop needs a protoAgent checkout in the bundle repo's CI (clone +
  `uv sync`, ~a minute). Acceptable for a weekly schedule + PR gate.

## Options considered

- **Float refs (track branch/HEAD).** Always fresh, never curated — one bad plugin push
  breaks every spawn, and `plugins.lock` reproducibility becomes a fiction. Rejected.
- **Pin-and-forget (status quo).** Reproducible but rots silently; this incident. Rejected.
- **Manifest-range + lockfile split (npm model).** A `ref-range:` in the manifest with a
  separate committed bundle-lock. Maximal machinery for marginal value at bundle scale
  (2–5 members, single curator) — tags + the verify-and-bump loop deliver the same
  freshness/known-good split with one file. Rejected.
- **Tags + verified_against + verify-and-bump CI (this decision).** The pin keeps ADR
  0040's guarantee; the lifecycle keeps it true.
