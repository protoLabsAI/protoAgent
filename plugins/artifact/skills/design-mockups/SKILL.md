---
name: design-mockups
description: When the user wants a design MOCKUP, a UI preview, a "what would this screen look like", or a reviewable prototype of a console view, panel or card — build it as an html artifact from REAL data with the design-system classes, a theme/width/state toggle bar and a render check at both widths, then hand the operator a browser link with open_artifact.
---

# Design mockups (a reviewable preview with a browser link)

A design mockup is a **panel artifact the operator reviews in a browser tab**: one html page
that shows a proposed screen in every state, at desktop and phone width, in light and dark,
with the design system's own components. You build it with `show_artifact`, prove it renders
with `check_artifact`, and hand it over with `open_artifact`, which returns a link that opens
the mockup full-page in a new tab.

For anything that isn't a screen to review (a chart, a diagram, a quick widget), use the
`rendering-artifacts` skill instead.

## The workflow

1. **Gather real data first.** Read what the screen would really show: call the API or tool
   that feeds it, read the files, look at a screenshot the user shared. Copy real names,
   counts, dates and messages into the mockup's `DATA`. Never lorem ipsum, never "Item 1",
   never "John Doe". If you can't reach the data, ask for a sample instead of inventing one.
2. **Build with the design system only.** Use the `pl-*` component classes (`pl-card`,
   `pl-btn`, `pl-badge`, `pl-alert`, `pl-callout`, `pl-empty`, `pl-skel`, `pl-field`,
   `pl-select`, `pl-segmented`/`pl-seg`, `pl-row`…) and the `--pl-*` tokens (`--pl-color-bg`,
   `--pl-color-fg`, `--pl-color-fg-muted`, `--pl-color-border`, `--pl-color-bg-raised`,
   `--pl-color-accent`, `--pl-color-status-error`, `--pl-space-1…12`, `--pl-radius`,
   `--pl-font-sans`, `--pl-font-mono`). The kit is already linked into every html artifact.
   Your own CSS is layout only: grid, flex, gaps, widths. No hex colours, no new fonts, no
   ad-hoc shadows. In a `react` artifact use the `@pl/ui` wrappers (`Card`, `Button`, `Badge`,
   `Alert`, `Skeleton`, `Tabs`…) for the same result.
3. **Start from the scaffold below.** It gives you the sticky toggle bar (theme, width, state),
   the desktop/phone frame, and the render check. Fill in `DATA` and one `STATES` entry per
   state.
4. **Cover every state.** At least: empty, one item, many items, loading, failed, and the edge
   cases the real data has (a very long name, a huge count, a missing field, an old date). Each
   state is a real, designed screen, not a grey box saying "error".
5. **Write human copy.** Plain sentences a person would say. "Nothing waiting for you" beats
   "No items found". An error says what happened and what to do next. No jargon, no ids the
   operator never sees, no exclamation marks. Information is never shown as a problem.
6. **Show it in the panel:** `show_artifact(kind="html", code=…, title="<Screen> mockup",
   placement="panel")`. A mockup is a work product, so it goes in the panel, not inline.
7. **Check the render:** `check_artifact(<id>)`. The scaffold measures every state at 1440 px
   and at 390 px while it loads, and fails the render when anything spills past the frame edge.
   So "rendered cleanly" means it mounted with no script error **and** nothing overflows at
   either width. A failure names the width, the state and the elements.
8. **Fix and re-check once.** Read the error, make a targeted `update_artifact`, then call
   `check_artifact` again. If it still fails, tell the operator what is left instead of looping.
9. **Hand over the link:** `open_artifact(<id>)`. With a console attached it opens a new tab;
   either way it returns a link that opens the mockup full-page. The link follows the browser's
   light or dark setting; add `&theme=light` or `&theme=dark` to force one. It is signed for
   that exact version and expires (the reply says when), so call `open_artifact` again for a
   fresh link rather than reusing an old one.
10. **Iterate on feedback with `update_artifact`** on the same artifact id, so the versions
    stack (v1, v2, v3) and the operator can step back through them. Check again, then
    `open_artifact` again: each version gets its own link.

## The scaffold

Copy this whole block as the artifact's `code` and fill in the two marked places. It is
theme-aware through the `--pl-*` tokens, and accessible: the toggles are `aria-pressed`
buttons in labelled groups, the state picker is a labelled `<select>`, and state changes are
announced in a live region.

```html
<style>
  .mk-bar{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:var(--pl-space-3);align-items:end;
    padding:var(--pl-space-3) var(--pl-space-4);background:var(--pl-color-bg-raised);border-bottom:1px solid var(--pl-color-border)}
  .mk-bar .pl-field{margin:0}
  .mk-stage{padding:var(--pl-space-4)}
  .mk-frame{box-sizing:border-box;width:100%;max-width:1440px;margin-inline:auto;overflow:hidden;
    border:1px solid var(--pl-color-border);border-radius:var(--pl-radius);background:var(--pl-color-bg)}
  .mk-frame[data-w="phone"]{max-width:390px}
  .mk-screen{display:grid;gap:var(--pl-space-3);padding:var(--pl-space-4)}
  .mk-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
</style>
<div class="mk-bar" role="toolbar" aria-label="Preview controls">
  <div class="pl-field"><span id="mk-t">Theme</span>
    <div class="pl-segmented" role="group" aria-labelledby="mk-t">
      <button type="button" class="pl-seg" data-theme-btn="system" aria-pressed="false">System</button>
      <button type="button" class="pl-seg" data-theme-btn="light" aria-pressed="false">Light</button>
      <button type="button" class="pl-seg" data-theme-btn="dark" aria-pressed="false">Dark</button>
    </div></div>
  <div class="pl-field"><span id="mk-w">Width</span>
    <div class="pl-segmented" role="group" aria-labelledby="mk-w">
      <button type="button" class="pl-seg" data-w-btn="desk" aria-pressed="false">Desktop 1440</button>
      <button type="button" class="pl-seg" data-w-btn="phone" aria-pressed="false">Phone 390</button>
    </div></div>
  <div class="pl-field"><label for="mk-state">State</label><select id="mk-state" class="pl-select"></select></div>
</div>
<div class="mk-stage"><section class="mk-frame" id="mk-frame" aria-label="Preview"><div class="mk-screen" id="mk-screen"></div></section></div>
<div class="mk-sr" role="status" id="mk-live"></div>
<script>
(() => {
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  // ① REAL data, copied from the system (an API response, a file, a screenshot). Never invented.
  const DATA = { items: [ /* { name: "…", when: "…", … } */ ] };

  // ② One entry per UI state; the first is the one shown on open. Each returns #mk-screen's markup.
  const STATES = {
    many:    { label: "Many",          render: () => DATA.items.map((i) => `<div class="pl-card">${esc(i.name)}</div>`).join("") },
    one:     { label: "One",           render: () => `<div class="pl-card">${esc(DATA.items[0]?.name)}</div>` },
    empty:   { label: "Empty",         render: () => `<div class="pl-empty"><p class="pl-empty__title">Nothing waiting for you</p></div>` },
    loading: { label: "Loading",       render: () => `<div class="pl-skel-group" aria-busy="true"><div class="pl-skel"></div><div class="pl-skel"></div></div>` },
    failed:  { label: "Couldn't load", render: () => `<div class="pl-alert pl-alert--error" role="alert"><div class="pl-alert__body"><p class="pl-alert__text">Couldn't load this. Check the connection, then try again.</p></div></div>` },
    // Edge cases come from the real data too: its longest name, biggest count, oldest date.
    long:    { label: "Edge: longest name", render: () => `<div class="pl-card">${esc([...DATA.items].sort((a, b) => String(b.name).length - String(a.name).length)[0]?.name)}</div>` },
  };

  const $ = (s) => document.querySelector(s), root = document.documentElement;
  const frame = $("#mk-frame"), screen = $("#mk-screen"), pick = $("#mk-state"), live = $("#mk-live");
  const press = (sel, on) => document.querySelectorAll(sel).forEach((b) => {
    const v = on(b); b.setAttribute("aria-pressed", String(v)); b.classList.toggle("pl-seg--active", v);
  });

  // Theme. In the console the shell writes its live --pl-* tokens onto <html style>, which beats
  // [data-theme]. A forced Light/Dark parks them; System removes data-theme and puts them back,
  // so System follows the viewer (the console, or the browser on the open_artifact page).
  let forced = null; const host = {};
  const park = () => { for (const p of [...root.style]) if (p.startsWith("--pl-")) { host[p] = root.style.getPropertyValue(p); root.style.removeProperty(p); } };
  new MutationObserver(() => { if (forced) park(); }).observe(root, { attributes: true, attributeFilter: ["style"] });
  function setTheme(t) {
    forced = t === "system" ? null : t;
    if (forced) { park(); root.setAttribute("data-theme", forced); }
    else { root.removeAttribute("data-theme"); for (const p in host) root.style.setProperty(p, host[p]); }
    press("[data-theme-btn]", (b) => b.dataset.themeBtn === t);
  }
  function setWidth(w) { frame.dataset.w = w; press("[data-w-btn]", (b) => b.dataset.wBtn === w); }
  function show(k) { pick.value = k; screen.innerHTML = STATES[k].render(); }

  for (const [k, s] of Object.entries(STATES)) pick.add(new Option(s.label, k));
  document.querySelectorAll("[data-theme-btn]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeBtn)));
  document.querySelectorAll("[data-w-btn]").forEach((b) => b.addEventListener("click", () => setWidth(b.dataset.wBtn)));
  pick.addEventListener("change", () => { show(pick.value); live.textContent = `Showing: ${STATES[pick.value].label}`; });

  // Render check: every state at exactly 1440 px and 390 px. Anything whose right edge passes the
  // frame (outside a scroll or clip box) fails the artifact's render, so check_artifact reports it.
  const name = (el) => el.tagName.toLowerCase() + (typeof el.className === "string" && el.className.trim() ? "." + el.className.trim().split(/\s+/).join(".") : "");
  const clipped = (el) => { for (let a = el.parentElement; a && a !== frame; a = a.parentElement) if (getComputedStyle(a).overflowX !== "visible") return true; return false; };
  const problems = [];
  for (const [w, px] of [["desk", 1440], ["phone", 390]]) {
    setWidth(w); frame.style.width = px + "px"; frame.style.maxWidth = "none";
    for (const k of Object.keys(STATES)) {
      show(k);
      const edge = frame.getBoundingClientRect().right + 1;
      const bad = [...screen.querySelectorAll("*")].filter((el) => el.getBoundingClientRect().right > edge && !clipped(el));
      if (bad.length) problems.push(`${px} px, ${STATES[k].label}: ${bad.slice(0, 3).map(name).join(", ")} past the edge`);
    }
  }
  frame.style.width = frame.style.maxWidth = "";
  setTheme("system"); setWidth("desk"); show(Object.keys(STATES)[0]);
  if (problems.length) (window.__artErr || console.error)("Layout check failed:\n" + problems.join("\n"));
})();
</script>
```

Notes on the scaffold:

- **Keep the toggle bar and the render check** even when you restyle everything else. The
  operator reviews with the bar; `check_artifact` relies on the check.
- **Desktop 1440** fills the column up to 1440 px; in a narrow panel it is as wide as the
  panel, and the open_artifact tab shows the real width. **Phone 390** is the 390 px frame.
- Something wide on purpose (a data table) belongs in its own `overflow-x:auto` box, which the
  check skips.
- **In React**, keep the same bar and states and run the same check from a `useLayoutEffect` in
  `App` on first mount (it runs before the artifact reports its render), using `@pl/ui` for the
  components.

## Writing the copy

- Say what the operator sees and can do, in their words: "3 decisions are waiting", "Sent to
  protoEngineer", "Couldn't send. Try again."
- Use the real names from `DATA` in the copy, so the mockup reads like the product.
- Empty states say what will appear there, and how to make it appear when that's useful.
- Keep labels short; put detail in a muted line under them, not in the label.

## Handing it over

Tell the operator in one or two sentences what the mockup shows (the screen and its states),
paste the `open_artifact` link, and ask for feedback. Say which data is real and where it came
from. Don't describe every state in chat: they can flip through them in the bar.
