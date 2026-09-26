// Escape arbitration for stacked DS Dialogs. The DS Dialog closes on EVERY Escape
// (a document keydown listener per open dialog), so with a dialog stacked on another —
// the New-agent set-up dialog over Settings, the folder picker over the set-up dialog —
// one press dismissed every layer at once. Until the DS dismisses only the top-most
// layer (protoContent#521), a dialog asks "am I on top?" before
// honouring an Escape.
//
// WHEN you ask matters (see SettingsOverlay, #2466): by the time a Dialog's own close
// handler runs, an inner layer that handled the same press may already be unmounted,
// so the answer is sampled on WINDOW capture — the first stop of the dispatch — and
// consumed one-shot by the close handler.

/** Is the `.pl-overlay` holding `dialogSelector` the last (top-most) one in the document? */
export function isTopmostOverlay(dialogSelector: string, doc: Document = document): boolean {
  const own = doc.querySelector(dialogSelector)?.closest(".pl-overlay");
  if (!own) return true;
  const all = doc.querySelectorAll(".pl-overlay");
  return all[all.length - 1] === own;
}

/** Is there an open interactive layer (dropdown/menu/listbox) above the dialog? (#2466)
 *
 *  Interactive layers only — a hovered TOOLTIP also rides a popper wrapper but must not
 *  hold the dialog open.
 *
 *  WHEN you ask this is the whole problem; see `SettingsOverlay`. */
export function escapeCloseAllowed(doc: Document = document): boolean {
  return (
    doc.querySelector(
      '[data-radix-popper-content-wrapper] :is([role="menu"],[role="listbox"],[role="dialog"])',
    ) === null
  );
}
