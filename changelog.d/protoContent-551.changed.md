- **Console: the three sanctioned composite raw-`<button>` controls carry the DS-audit `hand-rolled-control` line-level exception (protoContent#551).**
  The app-drawer surface rows, the app-drawer Settings row and the composer's model-menu
  trigger are deliberate composite controls (icon + label, and a menu trigger); designSystem
  ruled they stay raw `<button>`s rather than a DS primitive, and the audit heuristic is not
  being widened. Each now carries an inline `/* ds-audit-ignore hand-rolled-control … */`
  block comment so the design-system audit stops flagging them. Comments only — no markup,
  props, classNames, handlers or behaviour changed.
