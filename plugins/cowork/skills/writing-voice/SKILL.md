---
name: writing-voice
description: Learn the operator's writing voice from samples they provide and save it as a reusable style profile, so drafts written in their name sound like them. Use when the operator wants drafts to match their style, offers writing samples, or asks to set up/update their voice profile.
---

# Writing voice profile

Much of what this agent produces is prose the operator sends under their own
name. A voice profile turns "sounds like an AI" into "sounds like me".

## Consent first

Work only from writing the operator **authored and chose to share** — pasted
samples, or files/emails they explicitly point at. Never trawl connected
sources uninvited, and nothing saves without their review.

## 1. Collect

Ask for 3–5 samples of the kind of writing they send most (emails, updates,
proposals). More variety → better profile. If they have none handy, offer to
draft something together and iterate — the corrections teach the voice too.

## 2. Analyze

Extract what's *distinctive*, not generic: typical length and rhythm,
formality register, greeting/sign-off habits, how they open (context-first
vs ask-first), directness of asks, punctuation quirks (dashes, ellipses,
exclamation tolerance), words and phrases they reach for, words they'd never
use, emoji stance, how they soften or don't.

## 3. Draft the profile and get sign-off

Write the profile as concrete drafting instructions with 2–3 short example
lines in their voice. Show it. Revise until they say it's them.

## 4. Save

Save with `save_skill` as name `my-writing-style`, description "The
operator's personal writing voice — apply whenever drafting anything they
will send or publish under their own name." Body = the approved profile.
`save_skill` is additive-only: if `my-writing-style` already exists, don't
overwrite — show the existing profile and propose the revision to the
operator instead.

From then on, load and follow the profile whenever drafting in their name —
and mention you're using it the first time in each session, so they know
drafts aren't generic.
