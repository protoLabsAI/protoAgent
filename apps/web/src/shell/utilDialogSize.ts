/** One dialog width for every view the utility bar opens in a window (Feed, Work), so they
 *  open at the same size (Josh, 2026-11-12). Wide enough for Work's side-by-side list +
 *  inspector, which needs a 48rem container (work-view.css). The body height is shared too:
 *  `.util-dialog-fill` (Feed) and `.surface-modal--util` (Work), both min(60dvh, 560px). */
export const UTIL_DIALOG_WIDTH = "min(800px, 94vw)";
