- **`browser_select` resolves "Country" to a phone-country picker labelled only by aria-label (#4032).**
  A react-select / intl-tel-input phone-country picker on a Greenhouse form often has no
  `<label for>` — its only accessible name is `aria-label` / `aria-labelledby` on the select
  container or `.iti` wrapper (and on the listbox it controls). The in-page field enumeration now
  adds that accessible name as a label candidate for a label-less combobox/phone picker, so
  `browser_select(field="Country")` and `browser_form_read` address and report it by "Country"
  instead of failing with "no options were found". An explicit `<label for>` / wrapping `<label>`
  still wins, so a labelled field never borrows a neighbour's accessible name.
