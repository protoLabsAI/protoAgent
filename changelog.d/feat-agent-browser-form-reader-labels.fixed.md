- **Browser form tools read custom application questions by their question text.** In a
  live eval on 13 real job postings, `browser_form_read` lost the question on every Lever and
  Workable custom question. Fields came back as `cards[<uuid>][field0]`, `QA_12487827` or a UUID.
  Labels picked up junk such as "SVGs not supported by this browser.", "No location found…Loading"
  and glued-on descriptions. Palantir's 31 language checkboxes counted as 31 required fields.
  Fields with no label of their own now take the question from their container: Lever
  `.application-question`, Ashby's field heading, Workable's `aria-labelledby` radiogroup. Svg,
  hint and status text is dropped. A "select all that apply" list reads as one `checkbox-group`
  field with its options and ticked values. Palantir's form went from 62 rows with 19 junk labels
  to 30 rows with none.
- **`browser_select` ticks radio and checkbox options and picks autocomplete suggestions.**
  `browser_select("How did you hear about us?", "LinkedIn")` ticks that option (it never unticks).
  On a location box that suggests as you type (Lever, Google Places, an async combobox), it types
  the text, picks the unique matching suggestion with a real click and reads it back. If nothing
  appears, it retries once with the part before the first comma. If no suggestion matches, or
  several do, it lists them and restores the field. `option_text` also takes `|` alternatives
  (`"Decline to self-identify|I don't wish to answer"`), and the first one that matches wins.
  Discord had 10 failed selects over decline wording.
- **`browser_form_read` lists react-select options without committing.** It opens each
  unanswered menu, reads it and closes it again, and reads an unanswered one as `""` instead of
  `"Select..."`. Option lists over 60 entries are cut, with an `options_more` count.
- **`browser_fill` reads the value back.** A field that comes back blank or different is now an
  `Error:` with the actual value. Before, it reported success. A value the page only reformats
  (phone masks, thousands separators) still passes.
- **A CSS selector with brackets inside a quoted value is CSS.** For example,
  `input[name="cards[x][field0]"][value="Yes"]` used to be treated as a label and fail.
