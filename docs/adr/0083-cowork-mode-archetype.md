# 0083 — Cowork mode: a prepackaged knowledge-worker archetype

- Status: accepted
- Date: 2026-07-16
- Deciders: Josh, agent

## 1. Context & problem statement

Claude Cowork is Anthropic's agentic workspace for non-coders — verified by
teardown to be Claude Code headless in a sandbox VM. By mid-2026 it ships on
every paid plan, and Anthropic's own 1.2M-session study shows who lives there:
business operations (33.4%) and content creation (16.4%) dominate; software
development is 8.7%. The deliverables are files — spreadsheets, documents,
decks, PDFs — produced by four default office-document skills, plus a
`schedule` skill (distill the session into a self-contained prompt, run it on
a cadence), a `consolidate-memory` skill (merge/prune memory files), and a
guided `setup-cowork` onboarding (role → role-matched plugin packs → the
connectors those packs declare → try a skill → a saved writing-voice profile).

Those users hit verified ceilings that protoAgent already exceeds: cadence
limited to hourly/daily/weekly (no cron), cloud scheduled tasks that cannot
touch local folders, memory siloed per-surface and projects-only, no model
choice, usage limits that "burn faster than chat", and — since remote sessions
became the default — local files processed on Anthropic's servers. The
self-hosted pitch writes itself: same workflow, data never leaves the box.

But protoAgent has no landing pad for these users. Every archetype in the
new-agent picker that ships tools is developer-shaped (coder / PM / research);
`generic-assistant.md` exists as an orphaned soul preset with no catalog row
and no tools. A Cowork graduate today would have to hand-assemble plugins,
skills, folder fencing, and a persona before protoAgent resembles what they
left.

## 2. Decision drivers

- One-click: the existing archetype seams (ADR 0042) already deliver
  SOUL + bundle plugins + config defaults on both the first-run SetupWizard
  and the fleet new-agent panel — v1 must not require new core seams.
- Fidelity where it earns trust (file deliverables, folder consent,
  never-delete-without-asking, schedule + memory hygiene), divergence where
  we're strictly better (cron, local scheduled work, unified memory, model
  choice, plugins/fleet/A2A as the extension story instead of ZIP uploads).
- Legal cleanliness: Anthropic's document skills are all-rights-reserved
  (no redistribution, no derivatives, no extraction from the Services) even
  though the `anthropics/skills` repo is public.

## 3. Considered options

1. **Catalog archetype + bundle (chosen)** — a `cowork` soul preset + catalog
   row pointing at a `cowork-stack` bundle repo; skills ship inside a
   `cowork-plugin` listed by the bundle.
2. Fork-style "cowork edition" of protoAgent — rejected: modes are
   configuration, not forks; the operator-fork contract reserves forks for
   domain rewrites.
3. Core-seam-first (bundle `mcp:` field, env seeding, approval tiers before
   shipping) — rejected for v1: nothing in the package strictly needs them,
   and each is independently useful later.

## 4. Decision

- **D1 — Ship as data, zero core seams.** `config/soul-presets/cowork.md`
  (persona written from the usage data: ops/content copilot, file
  deliverables, folder-fenced, memory-forward, never delete without asking)
  plus one `archetype-catalog.json` row (`id: cowork`, `bundle:`
  `https://github.com/protoLabsAI/cowork-stack`, `soul_preset: cowork`),
  inserted before `custom`.
- **D2 — `cowork-stack` bundle composition.** `cowork-plugin` (the skill
  pack) + `artifact` + `notes` + `execute_code` (builtins; the document
  skills need a runtime — the same reason Cowork carries a VM) +
  `google-plugin` (Gmail/Calendar/Drive ≈ the connector layer) +
  `claude-bridge` (imports the user's existing Claude Code/Cowork state).
  Curated `enabled` list + config defaults (operator fs tools fenced to
  user-chosen folders, scheduler on).
- **D3 — Original document skills, not Anthropic's.** `cowork-plugin` ships
  clean-room `docx`/`xlsx`/`pptx`/`pdf` skills over the standard Python
  libraries (`python-docx`, `openpyxl`, `python-pptx`, `pypdf`), declared via
  `requires_pip`. Vendoring Anthropic's skills is prohibited by their
  license; that finding is recorded here so nobody re-litigates it.
- **D4 — Migration respects the same license.** The claude-bridge skill
  importer must skip `creatorType: "anthropic"` skills and migrate only
  user-authored ones (including `my-writing-style`).
- **D5 — Deferred seams, filed as issues.** A bundle `mcp:` field, a
  read-write-no-delete fence mode, per-action approval tiers (ties into the
  ADR 0071 consent-acks follow-up), and env/secrets seeding from archetypes.

## 5. Consequences

- Good: Cowork's entire prepackaged experience becomes reproducible
  self-hosted in one click, and the archetype doubles as the template for
  future "modes" (a role pack is just another bundle + preset).
- Trade-off: until D5's seams exist, Google OAuth and any MCP servers are
  wired up in the console after install rather than seeded by the archetype —
  the onboarding skill walks the user through it, mirroring `setup-cowork`.
- Risk: the doc skills are original prompts over commodity libraries; parity
  with Anthropic's tuned skills is asymptotic, not guaranteed. Evals (ADR
  0012) can close the gap iteratively.
