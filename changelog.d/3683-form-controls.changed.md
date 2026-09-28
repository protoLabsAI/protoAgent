- **Remaining hand-rolled form controls move onto the design system (#3683).** The Skills
  editor's two raw `<input type="checkbox">` (invokable-as-slash and its user-only companion)
  become the DS `Checkbox` from `@protolabsai/ui/forms`, and the workflow gate editors' raw
  `<textarea className="workflow-gate-edit">` (PendingGateCard + the inline run-timeline gate)
  become the DS `Textarea`. Behaviour is unchanged: each checkbox keeps its `aria-label`
  accessible name and its visible `/slash` label, unchecking the slash trigger still clears
  "user only", and each textarea keeps its `workflow-gate-edit` styling, `edited prompt`
  accessible name, `rows`, value and change handler. Separately, the activity feed's local
  `Badge` helper — which shadowed the DS `Badge` name yet only renders the provenance row — is
  renamed `OriginProvenance`; its markup is unchanged.
