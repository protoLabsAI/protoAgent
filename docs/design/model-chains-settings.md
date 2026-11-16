# Model fallback chains in Settings (ADR 0133 M7)

Status: **Approved by Josh 2026-11-16 (preview v8, "that will work for the first pass").** M7a–c boarded.
Source: protoAgent ADR 0133, "Every core model role takes a fallback chain, set in config" (signed off by Josh; PR #5360, amended by PR #5370 for image describe and the subagents line). Josh: "need to make sure its exposed in the ui and not just config."
Preview: artifact `a-1794808722879-bbe41a` (before and after, at 1440 and 390, light and dark).

## 1. What exists today

These facts come from protoAgent main, read on 2026-11-16.

- **Settings › Model** is one section (`apps/web/src/settings/sections.ts`, id `model`). It renders accordion groups: Connections, Model & runtime, Routing, Compaction, and others.
- **Main model:** `model.name` is a `select` field named "Primary model" in Model & runtime (`src/graph/settings_schema.py` ~123).
- **Main fallbacks:** `routing.fallback_models` is a `string_list` in the Routing group (~393). It renders as a column of `Combobox` rows, with up and down buttons, a blank row for adding, and clear-to-remove (`SettingsCategory.tsx` ~800–835, `moveListItem`).
- **Aux:** `routing.aux_model` is a single `Combobox` in Routing (~381).
- **Other roles** are single fields spread across groups:
  - `compaction.model` "Summarizer model" in Compaction (~448)
  - `goal.eval_model` "Verifier model" in Goal mode (~511)
  - `knowledge.image_describe_model` in Knowledge (~907)
  - subagents in a free-form `subagents` dict field (~179)
- **The subagent registry is already served:** `GET /api/subagents` (`src/operator_api/subagents.py`, `list_subagents`) lists every entry in the agent's `SUBAGENT_REGISTRY`. The console reads it through `subagentsQuery` (`apps/web/src/lib/queries.ts` ~301). The list differs per agent, because plugins and archetypes register their own subagents.
- **No Settings field yet:** harvest, facts, memory select, chat gist and drift judge (`soul.drift.judge.model` exists in config only).

## 2. Decisions

1. **One group, "Model roles", in Settings › Model, between Connections and Favorites.** It replaces the Routing group's two model fields, "Primary model" in Model & runtime, Compaction's "Summarizer model" and Goal mode's "Verifier model". Image description moves here from Knowledge.
   *Reason:* the inheritance rule (specific role → aux → main) only reads well when the parent sits above the child on one screen. Today's split hides that compaction inherits aux, and that aux inherits main.
2. **Every role uses one chain control.** The control is today's `fallback_models` list, made general: `Combobox` rows from `slot_models`, with ↑ ↓ and a ✕ remove button on each row, and "+ Add fallback" under the list. Rows are labelled "Primary", then "then". On a phone they are labelled 1, 2, 3.
   - Adding a model uses a button, not a blank row. Today's blank trailing row looks like an unset value, and a blank value now means "inherit".
   - ✕ replaces clear-to-remove, because an empty combobox is ambiguous.
   *Reason:* the ordered-list pattern already exists in Settings and the board's coder ladders work the same way. A new pattern would be a fork.
3. **Main model plus its fallbacks is ONE chain.** The control writes `model.name` as a list. The console never writes `routing.fallback_models` again. If an older config still has values there, the control shows them as rows after the primary, and Save writes them into `model.name`. The main chain can't be emptied (✕ is disabled on its last row).
4. **A blank role is one quiet line that names only its parent: "Inherits aux"** (or "Inherits main", "Inherits harvest"), in muted text (revision v8, Josh 2026-11-16: "feels a bit crowded with all of the fell back to... and then the inherits x: x > x > x").
   - **The chain isn't repeated.** It appears once, in full, on the role it comes from. The "Inherits aux" text is a toggletip: a ghost `Button` with a dotted underline that opens a `Popover` (`@protolabsai/ui/overlays`) on click, tap, Enter or Space. The popover lists the full resolved chain, one full id per line, for example "Inherits harvest, which inherits aux's chain: 1. anthropic-oauth:claude-haiku-5-5 …". Escape closes it and returns focus to the trigger.
   - *Why a Popover and not a `Tooltip`:* a tooltip opens only on hover or focus, so a phone can't reach it. A toggletip works the same everywhere.
   - **"Set its own chain" is a quiet `ghost` `sm` text button** after the inherits text. On desktop it is hidden (opacity 0) until the row is hovered or has focus, so it still takes keyboard focus in tab order and shows up when focused. On phone, or any `(hover: none)` device, it is always shown, at 44 px.
   - Set its own chain opens the editor, pre-filled with the inherited chain (so a small edit stays small).
   - Once a role has its own chain, the editor shows a **Use aux's chain** button (or "Use main's chain" for aux). It clears the field.
   - Facts reads "Inherits harvest" while harvest is blank (ADR 0133 D5). Its toggletip says "Inherits harvest, which inherits aux's chain" and lists the chain.
   - **Image describe inherits the main chain, not aux** (Josh, 2026-11-16; ADR 0133 as amended by #5370). A blank value reads "Inherits main", with the main chain in its toggletip, and its editor's reset button reads "Use main's chain". *Reason:* aux models often can't read images, but the main model usually can.
   - **If no model in the resolved image chain can read images,** the role gets the same short status as a fallback (item 6): a `warning` dot on the role name and a muted "unavailable" toggletip under it. The toggletip says "None of these models can read images. Set its own chain with a vision model." The signal comes from the server (M6's role status reports the role `unavailable`, with the reason `no-vision`). The console doesn't guess which models can read images. Without that signal, no line shows.
   *Reason:* Josh asked for inheritance to be visible. One line per inherited role keeps 10 of the 11 rows short.
5. **No silent fall-through, said once.** The main chain's help text says: "Fallbacks are tried in order when a model is rate-limited, out of quota or down. A bad request is never retried." The other roles carry no repeat of this text. A set chain shows only what it lists, which tells the reader the same thing (ADR 0133 D2).
6. **Last fallback, per role, from M6: a dot and three words** (revision v8).
   - The role name gets a `warning` `StatusDot`. Under the name, in the label column, a muted toggletip reads "fell back 2h ago". Its popover holds the full event: "openai-codex:gpt-6-luna answered 2h ago; anthropic-oauth:claude-haiku-5-5 was rate-limited." Without a fallback, the role shows nothing.
   - **An event is printed once, on the role that owns the chain.** When an inheriting role fell back for the same reason as its source (the same resolved chain, the same failed model and reason, within the same 24 hours), the source role carries the "fell back…" text. The inheriting role shows only the dot, which is itself the toggletip trigger, named "&lt;role&gt; fell back 2h ago, the same as aux. Details". Its popover says "Same fallback as aux." followed by the full event.
   - If an inheriting role fell back for a different reason, it gets its own "fell back &lt;age&gt; ago" text.
   - The dot is `aria-hidden` when the text is beside it. When the dot is the only mark, its button carries the words.
   - The line clears after 24 hours with no new fallback (approved by Josh, 2026-11-16).
   - If the status API fails, the group shows one muted line: "Fallback history isn't available right now. Chains still work." The chains stay editable.
7. **Grouping, in this order, with small subheads inside the one group:**
   - Main and aux
   - Memory: compaction, harvest, facts, memory select, chat gist
   - Checks: goal eval, drift judge
   - Media: image describe
   - Subagents
   The group opens by default. Every inherited role is one short line. In the preview at 1440, the group's body went from 1265 px in v7 to 1058 px in v8.
   - **Inherited roles are not merged into one row** (Josh asked me to consider it, in v8). The preview shows the merged row ("Compaction · Harvest · Facts · Memory select · Chat gist: Inherit aux") as an alternative I don't recommend. *Reasons:* editing one role takes two steps (open a menu, then pick the role). The merged row hides that Facts inherits Harvest. And it saves only about four short rows once each row is already one quiet line.
8. **Subagents are one collapsed line, built from the agent's own registry** (approved by Josh, 2026-11-16: never a fixed list).
   - **Source:** the rows and the counts come from `subagentsQuery` (`GET /api/subagents`) on the agent being configured. Nothing in the console names a subagent. The 21 names in the preview are this agent's registry today, used only as sample data.
   - **Each subagent's chain** is `subagents.<name>.model`. A subagent counts as having its own chain when that value is set. Otherwise it reads "Follows the chat's /model pick, else aux", which is `subagent_model.py`'s real order: pin, then turn override, then aux.
   - **Copy by count** (N registered, K with their own chain, M = N − K):
     - **0 subagents:** the Subagents subhead and the line are both hidden.
     - **1 subagent:** "1 subagent (coder) · has its own chain", or "1 subagent (researcher) · follows the chat's model, else aux".
     - **N subagents:** "N subagents · K have their own chain · M follow the chat's model, else aux". Use "1 has" when K = 1, and leave out the "K have…" part when K = 0.
   - The line is a `Button` with `aria-expanded` and `aria-controls`. It expands to one row per registry entry, in registry order. A subagent the registry marks `enabled: false` still gets a row, with "(off)" after its name.
   - The list re-reads the registry when Settings opens, so a newly installed plugin's subagent shows up without a code change.
   - *Reason:* 21 rows would bury the eight core roles. And since each agent's registry is different, a fixed list would be wrong on every other agent.
9. **An entry not on any connection** is kept in the list. It gets a muted warning row under it: "Not a model on any of your connections. It will be skipped until it is." This matches ADR 0133 D6, where the loader drops such an entry with a log line. The UI doesn't delete what the operator typed.
10. **Saving** works like every other Settings group: an "N unsaved" badge on the group header, and Discard and Save. Changes apply live (ADR 0133 D6).
11. **Phone rule (below 768 px): model ids are never cut off** (Josh, 2026-11-16: "labels are cutoff for the models on the mobile width").
   - **Chain rows become small cards, not inline inputs.** Each card shows the position ("1 · Primary", "2 · Fallback"), then the connection prefix in muted mono on its own line (`anthropic-oauth`), then the model name in body mono (`claude-haiku-5-5`). Both lines wrap (`overflow-wrap: anywhere`, with break points after `:` and `/`). An id with no prefix (`protolabs/fast`) shows only the name line.
   - **Controls get their own line under the id:** Change, ↑, ↓ and ✕, right-aligned, each at least 44 × 44 px. The id never shares a line with a control, so the controls can't squeeze it.
   - **Change opens the picker in place:** a full-width `Combobox` with Cancel and Done. While it is open, the full id has the whole card width.
   - **Inherited line:** "Inherits aux" and Set its own chain, both always visible and 44 px tall. Tapping "Inherits aux" opens the popover with the full chain, one full id per line, wrapping.
   - **Fallback status:** the dot on the role name and "fell back 2h ago" under it, both 44 px tap targets. The popover names full ids and wraps.
   - **Popovers** are at most the card's width and wrap their text. They never clip an id.
   - **Role labels** stack above the chain at full width and wrap. They are never truncated.
   - **Desktop keeps one row per model,** with an input at least 22 characters wide.
   - **Rule:** nothing in this group uses `text-overflow: ellipsis` or clips a model id at any width. If space runs out, the text wraps.
12. **No design-system change.** The pieces are `Combobox`, `Button` (`ghost`, `sm`), `StatusDot` (`data`), `Popover` (`overlays`, for the toggletips), `AccordionItem` (the existing group), and `Badge`. The DS has no "toggletip" component, so M7b composes `Popover` and a ghost `Button`. If a second surface needs the same pattern, it's worth proposing as a DS component then, not now. A drag handle isn't needed, because ↑ ↓ is today's pattern and works by keyboard.

### Settled in ADR 0133 (PR #5370)

- A blank image describe inherits **main** (§2 item 4).
- A blank drift judge and a blank memory select inherit **aux**.
- Subagents are built from the registry (§2 item 8).

**One dependency for M6:** the role status has to report image describe as `unavailable` with the reason `no-vision` when no model in its resolved chain can read images. M7c shows that state but can't work it out itself.

## 3. Accessibility

- Each chain is `role="group"` with the accessible name "&lt;role&gt; model chain".
- Each combobox is named "&lt;role&gt; model N of M".
- The buttons are named "Move &lt;model&gt; up", "Move &lt;model&gt; down" and "Remove &lt;model&gt;". Boundary buttons are disabled, not hidden, so the row layout stays stable.
- After a move, focus stays on the moved row's button. A polite live region says "&lt;model&gt; moved to position N".
- The fallback line uses words as well as the dot. The dot is `aria-hidden`.
- On a phone (below 768 px), every button and combobox is at least 44 px tall. Rows stack the role name above the chain.
- Contrast: muted text uses `--pl-color-fg-muted` on `--pl-color-bg-raised`, the pair Settings already uses for help text.

## 4. States and copy

| State | Copy |
|---|---|
| Inherited role | "Inherits aux" (toggletip: "Inherits aux's chain:" + numbered full ids) · quiet button "Set its own chain" |
| Own chain | Rows "Primary", "then"… · "+ Add fallback" · "Use aux's chain" |
| Main chain help | "Fallbacks are tried in order when a model is rate-limited, out of quota or down. A bad request is never retried." |
| Last fallback, source role | Dot on the name · "fell back &lt;age&gt; ago" (toggletip: "&lt;model&gt; answered &lt;age&gt; ago; &lt;failed model&gt; &lt;reason&gt;.") Reasons: "was rate-limited", "was out of quota", "was down", "timed out" |
| Last fallback, same as source | Dot only (toggletip: "Same fallback as &lt;source&gt;." + the full event) |
| No fallback | Nothing |
| Status unavailable | "Fallback history isn't available right now. Chains still work." |
| Unknown model | "Not a model on any of your connections. It will be skipped until it is." |
| Subagents, 0 registered | Subhead and line hidden |
| Subagents, 1 registered | "1 subagent (&lt;name&gt;) · has its own chain" or "1 subagent (&lt;name&gt;) · follows the chat's model, else aux" |
| Subagents, N registered | "N subagents · K have their own chain · M follow the chat's model, else aux" (from the registry) |
| Image describe, blank | "Inherits main" (toggletip lists the main chain) · note "needs a model that can read images" |
| Image describe unavailable | Dot on the name · "unavailable" (toggletip: "None of these models can read images. Set its own chain with a vision model.") |

## 5. Build slices (M7, each at most 4 files, changelog excluded)

**M7a: the schema describes roles as chains** (depends on ADR 0133 M1)
- Files: `src/graph/settings_schema.py`, `tests/test_settings_schema.py`, `apps/web/src/lib/types.ts`.
- Acceptance criteria:
  1. A new field type `model_chain` exists. Its value is a list of strings. A stored string reads as a one-item list, and blank reads as `[]`.
  2. Every ADR 0133 D5 role has one `model_chain` field in section "Model roles", category Model. Each field carries `role`, `subhead` (Main and aux / Memory / Checks / Media) and `inherits` (the parent role id: empty for main, `main` for aux and image describe, `harvest` for facts, `aux` for the rest). These are main (`model.name`), aux, compaction, harvest, facts, goal eval, memory select, chat gist, drift judge and image describe.
  3. The old single fields (Primary model, Auxiliary model, Fallback models, Summarizer model, Verifier model, Image description model) stay in `FIELDS` with `ui_hidden=True`, so YAML and the roundtrip still work.
  4. Saving `model.name` as a list writes a list. Saving `[]` on a non-main role writes blank (inherit).
  5. `SettingsField` in `types.ts` gains `role?`, `subhead?` and `inherits?`.
  6. No subagent name appears in `settings_schema.py`. Subagent chains stay under the existing `subagents` dict and are written as `subagents.<name>.model`, either a string or a list.
  7. Tests that fail on main: the field-type, section and inherits cases, including `inherits == "main"` for image describe.

**M7b: the chain control** (depends on M7a)
- Files: `apps/web/src/settings/ModelChainInput.tsx` (new), `apps/web/src/settings/ModelChainInput.test.tsx` (new), `apps/web/src/settings/SettingsCategory.tsx` (route `model_chain` to it), `apps/web/src/settings/settings.css`.
- Acceptance criteria:
  1. A chain renders one `Combobox` row per model, labelled "Primary" then "then". Each row has ↑ ↓ ✕ with the accessible names in §3. The ↑ on the first row and the ↓ on the last row are disabled.
  2. "+ Add fallback" appends an empty row and focuses it. An empty row is dropped on change, not saved.
  3. Reorder uses `moveListItem`. Focus stays on the moved row's button, and a live region announces the new position.
  4. A blank inheriting role renders only "Inherits &lt;parent&gt;". No model id appears in the row. The text is a toggletip button (`aria-expanded`, `aria-controls`) that opens a `Popover` on click, tap, Enter or Space and closes on Escape, returning focus. The popover lists the full chain resolved from the parent's current value, including unsaved edits. "Set its own chain" is a `ghost` `sm` button: on desktop it is at opacity 0 until the row is hovered or focused, and it stays in tab order; below 768 px, or on `(hover: none)`, it is always visible. "Set its own chain" pre-fills the parent's chain. "Use &lt;parent&gt;'s chain" clears the field.
  5. The main chain's last ✕ is disabled. When the config has legacy `routing.fallback_models`, they show after the primary, and the next save folds them into `model.name`.
  6. An entry not in `field.options` shows the unknown-model warning and is kept.
  7. Below 768 px, the buttons and comboboxes are at least 44 px tall. The CSS uses only `--pl-*` tokens, and the token guard passes.
  8. Below 768 px, each chain row renders as a card (§2 item 11). The card shows the connection prefix and the model name as wrapping text on separate lines, with Change, ↑, ↓ and ✕ on their own line underneath. Change opens a full-width `Combobox` with Cancel and Done. The inherited toggletip's popover is at most the row's width and lists one full id per item.
  9. No element in the control uses `text-overflow: ellipsis`. At 390 px, with the id `anthropic-oauth:claude-haiku-5-5`, every id text's `scrollWidth` is at most its `clientWidth`.
  10. Tests that fail on main: criteria 1–6, 8 and 9.

**M7c: the Model roles group and the fallback status** (depends on M7b and ADR 0133 M6)
- Files: `apps/web/src/settings/ModelRolesGroup.tsx` (new), `apps/web/src/settings/ModelRolesGroup.test.tsx` (new), `apps/web/src/settings/SettingsCategory.tsx` (render the "Model roles" section with it), `apps/web/src/lib/queries.ts` (the role-status query on M6's endpoint).
- Acceptance criteria:
  1. The group renders the subheads in §2 item 7, in that order, between Connections and Favorites.
  2. **Subagents come from the registry.** The group builds the subagent line and its rows from `subagentsQuery` (`GET /api/subagents`), in registry order. The source has no hard-coded subagent names, and a test fails if one appears.
     - Registry with 0 entries: no Subagents subhead and no line.
     - Registry with 1 entry: the line reads "1 subagent (&lt;name&gt;) · …", per §4.
     - Registry with 3 entries, 1 of them pinned: the line reads "3 subagents · 1 has its own chain · 2 follow the chat's model, else aux".
     - Registry with 2 entries, none pinned: "2 subagents · 2 follow the chat's model, else aux".
     - The line is collapsed by default, with `aria-expanded="false"`. Activating it shows one row per registry entry. A disabled entry's row is marked "(off)".
     - The test fixtures use names that aren't in this repo's registry (for example `alpha`, `beta`), to prove nothing is hard-coded.
  2a. **Image describe.** When blank, it reads "Inherits main". When the role status reports `unavailable` with the reason `no-vision`, it shows the dot and the "unavailable" toggletip from §4. Without that status, nothing shows.
  3. A role with a fallback event in the last 24 hours shows a `warning` `StatusDot` on its name and the muted toggletip "fell back &lt;age&gt; ago". The full event, with full model ids, is only in the popover, and it wraps at 390 px. A role with none shows nothing.
  3a. **Each event is printed once.** Given aux and compaction (inheriting aux) with the same event, aux shows "fell back 2h ago" and compaction shows only the dot button, whose accessible name contains "the same as aux". The full event text appears zero times in the closed group. Given compaction with a different event, compaction shows its own "fell back &lt;age&gt; ago".
  4. A failed status query shows the one "Fallback history isn't available" line, and every chain stays editable.
  5. Facts reads "Inherits harvest" while harvest is blank, and its popover says "which inherits aux's chain".
  6. Tests that fail on main: criteria 1–5 and 3a, including every registry-size case in 2 and both image describe cases in 2a.

M7c reads the existing `subagentsQuery`, so it adds no new server route for subagents. M7c's `queries.ts` change adds only the role-status query.

M7b and M7c both touch `SettingsCategory.tsx`, so they run in order (M7c `depends_on` M7b).

## 6. Decided (Josh, 2026-11-16)

1. **Placement:** one "Model roles" group in Settings › Model. Approved.
2. **Subagents:** one collapsed line with counts, built dynamically from the agent's own subagent registry. The line is hidden at 0 subagents and has its own wording for 1 (§2 item 8). Approved with that requirement.
3. **Fallback line:** stays for 24 hours. Approved.
4. **Image describe:** changed from my recommendation. A blank value inherits the **main** chain. It is shown as unavailable when no model in that chain can read images (§2 item 4).

5. **Calmer pass (v8):** Josh: "feels a bit crowded with all of the fell back to... and then the inherits x: x > x > x". The result is §2 items 4, 6 and 7: inherited rows name only the parent, the chain and the fallback detail move into toggletips, "Set its own chain" is quiet, and each event is printed once. I'm not recommending the merged inherited row.

Still open: Josh's approval of v8 (including the phone-width rules in §2 item 11). After that, M7 can be carded.
