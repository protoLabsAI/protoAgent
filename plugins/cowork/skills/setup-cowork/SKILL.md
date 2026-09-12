---
name: setup-cowork
description: Conversational first-run companion for the Cowork archetype — verify the workspace is ready, import existing Claude Code/Cowork state, prove the setup on a real task, learn the operator's writing voice, and wire the first scheduled task. The mechanical setup (archetype, plugins, dependencies, folders) belongs to the setup wizard; this skill picks up where it leaves off.
user_facing: true
slash: setup-cowork
---

# Cowork first conversation

The setup wizard already handled the mechanical work — archetype, bundle,
persona. This skill is the part that has to happen *in conversation*: proving
the setup on the operator's real work and personalizing it. One step at a
time, every step skippable, a few sentences per message. If they detour into
real work, help, then resume.

## 1. Readiness check (silent, then one line)

Before anything user-facing, verify the workspace is actually ready:
document-library packages importable (try `execute_code` with a bare
`import docx, openpyxl, pptx, pypdf`), fenced folders configured, Google
plugin connected if enabled. Don't walk the operator through fixing gaps in
chat — the console owns that. One line each, only for what's missing:
dependencies → Settings ▸ Plugins ▸ cowork ▸ install-deps; folders →
Settings ▸ Tools; Google → Settings ▸ Plugins. If everything's ready, say
nothing about any of it and start at step 2.

## 2. Import what they already have

If the claude-bridge plugin is enabled, offer to look at their existing
Claude Code/Cowork state: memory for context, and any **user-authored**
skills worth migrating (Anthropic's bundled skills are licensed to their
services and are never imported — this pack's document skills replace them).
Summarize what you find before importing anything; import only what they
approve.

## 3. Prove it on their real work

Ask for one genuine task — a messy folder, a spreadsheet that needs
reconciling, a doc to draft — and run it end to end with the relevant
document skill. Name the file you produced. This one deliverable teaches
more than any tour.

The first time one of their folders is in play, mention the protection
modes in one breath: each fenced folder is read-only, read-write, or
read-write-no-delete (Settings ▸ Tools), and deleting a file always asks
them first whatever the mode. Folders of originals do well as no-delete;
output folders as read-write. Say it once, in passing — don't turn it
into a lecture.

## 4. Writing voice

Offer the writing-voice skill: it reads samples they choose to share, takes
about two minutes, and nothing saves without their review. Skipping is fine
— "whenever you want drafts to sound like you, just ask." If a profile
already exists, say so and move on; never overwrite one.

## 5. Wrap with the habit

Show what recurring work looks like: "Anything we did today can run on a
schedule — say 'every Friday' and it happens." If a natural candidate came
up, offer to schedule it now (schedule skill). Close: type `/` to see
skills, and ask for anything the way they'd ask a colleague.
