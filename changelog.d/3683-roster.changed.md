- **Fleet Room roster + diagnostics controls move onto the design system (#3683).** The last raw
  `<button>`s and `<input>` in the Fleet Room are replaced by the DS `Button` and `Input`: the
  roster member button, the per-row start/stop, diagnostics and open-console icon buttons, the
  @-mention list items, and the diagnostics drawer's Back, Retry, Refresh and Inspect buttons plus
  the task-id field. Refresh and Inspect now use `Button`'s `loading` prop while their read is in
  flight (spinner + disabled), and the field/button chrome — padding, border, background, radius,
  hover and focus rings — is owned by the DS instead of hand-rolled CSS. Behaviour, disabled
  conditions and accessible names are unchanged. Completes #3683.
