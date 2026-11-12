- **Work's New flow now confirms, edits and saves — directly (bd-c8r5h).** The step-2
  "Check this before saving" form shows the agent's plain reading, where it lands
  ("Goes in *group* as *tag*"), and the approved editable fields for its kind: a Job's
  What/Who (this agent or a delegate) starting now; a Time follow-up's First run, human
  Repeat choices (never cron — an unmapped repeat shows a sentence plus ← Change the
  words) and Then; a Condition's When, registered check, cadence, reaction, once/each
  and stop-after; an Instruction's read-only "Your words" (always the operator's typed
  bytes, never the model's copy), scope and conditions. Save calls each primitive's
  existing API in the current chat's context with no second agent turn; a Job starts as
  one normal chat turn ("Start: …", or `@delegate Start: …` for a delegation). Every
  Time/Condition/Instruction/Approval inspector now opens the same prefilled form as its
  first action, Edit — a condition edit PATCHes the original watch in place, keeping its
  id, history and check count. A successful save opens the new item in its group and
  announces "Saved to *group*." exactly once; a failed save keeps the input, and a retry
  or double click can never create a duplicate. At phone width the confirm and Edit steps
  are the same bottom sheet as step 1, with no horizontal overflow at 390 px.
