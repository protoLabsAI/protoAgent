# Bundle template — the pin lifecycle baked in (ADR 0049)

A reference layout for a protoAgent **plugin bundle** repo whose pins stay honest. A
bundle (ADR 0040) is a reference manifest over standalone plugin repos — its whole value
is "this combo was verified together". This template adds the lifecycle that keeps that
claim true after authoring day:

| File | Role |
|---|---|
| `protoagent.bundle.yaml` | The manifest — tag pins + `verified_against:` (the rules are commented inline) |
| `scripts/verify_bundle.py` | Installs the pin set into a scratch agent, loads every member, probes every declared console view for 200 |
| `scripts/check_bundle_updates.py` | Rewrites tag pins to the newest release tag (comment-preserving) |
| `.github/workflows/verify-bundle.yml` | Wires both into CI: verify on every PR + weekly; auto-open pin-bump PRs |

## The core rules

1. **Pin release tags, not raw SHAs.** Legible, advisable (the console's update check can
   ls-remote a tag), and it nudges plugin repos to cut releases. The installer still locks
   the exact commit SHA — reproducibility is unchanged.
2. **Record `verified_against:`** — the core version this pin set was last verified on.
3. **A pin only moves through a passing verification.** Hand-edit or bot-bump, either way
   the PR must pass `verify` — which installs the manifest for real and probes every
   declared view. The weekly schedule re-runs it so silent rot turns a badge red.

## Using it

```bash
# Start a bundle repo from this template
cp -r examples/bundles/template my-stack && cd my-stack && git init

# Verify locally (from a protoAgent checkout with deps synced)
uv run --no-sync python /path/to/my-stack/scripts/verify_bundle.py /path/to/my-stack

# Check for newer member releases
python3 scripts/check_bundle_updates.py protoagent.bundle.yaml
```

Why this exists: the first real bundle shipped pins that predated both members' view
fixes — every agent spawned from the archetype got 404 panels out of the box, and nothing
flagged it. The verify probe above catches exactly that, at authoring time and weekly
thereafter. Full rationale: [ADR 0049](../../../docs/adr/0049-bundle-pin-lifecycle.md);
the live adopter is [pm-stack](https://github.com/protoLabsAI/pm-stack).
