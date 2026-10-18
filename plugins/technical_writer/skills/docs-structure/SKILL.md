---
name: docs-structure
description: Use when improving documentation information architecture, audience paths, navigation, page ownership, or reorganizing sections.
---

# Organize around reader tasks

1. Map current entry points and page ownership. Identify audiences from the product and
   request, then trace first success, routine use, troubleshooting, building/extending,
   and operating paths as applicable. Do not impose this app's audience order on every repo.
2. Find competing owners, missing transitions, orphaned pages, overloaded hubs, and
   navigation labels that require implementation knowledge. Distinguish public docs
   from maintainer plans, ADR history, and generated reference.
3. Propose the smallest useful map: audience/task entry points, canonical destinations,
   hub summaries, and cross-links. Each page has one primary home; secondary paths link
   there. Keep task guides distinct from reference and conceptual explanations.
4. Record a migration table: old path/anchor, new owner, redirect or compatibility anchor,
   inbound links to update. Prefer navigation/link changes over URL moves without benefit.
   If a structural decision needs user input, prepare the concrete map while awaiting it.
5. Implement authorized changes in ordered slices. Update all navigation consumers,
   generated catalogs, indexes, and local links from their owning source. Check both
   existing URLs and new reader paths. Use `docs-verify` before delivery.

Done when each intended audience has a discoverable route to its tasks, ownership is
unambiguous, and moves preserve published links or explicitly document migration limits.
