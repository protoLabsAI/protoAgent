// @pl/ui — thin authored React wrappers over the protoLabs design-system `.pl-*` classes
// (the DS ships .tsx source, not browser ESM, so we can't import its real components into
// the sandbox; these mirror the class contracts and share the artifact's React instance via
// the import map). Pair with the injected plugin-kit.css so they pick up the live theme.
// Icons are backed by the vendored lucide icon data — no separate icon dependency.
//
// The plugin-kit.css injected into every artifact already carries the DS styling for ~90
// `.pl-*` components; these wrappers give the ergonomic React surface for the layout,
// structure, navigation, data, overlay, form and typography pieces an agent needs to
// prototype real LAYOUTS and NEW COMPONENTS — not just a handful of primitives. Each is a
// thin, predictable className contract; drop to a raw element + `className="pl-…"` for the
// long tail this file doesn't wrap.
import React from "react";
import { icons as lucideIcons } from "lucide";
const h = React.createElement;
const cx = (...a) => a.filter(Boolean).join(" ");
const mod = (base, variant, size) => cx(base, variant && `${base}--${variant}`, size && `${base}--${size}`);

// ── Primitives ────────────────────────────────────────────────────────────────

export function Button({ variant, size, className, children, ...rest }) {
  return h("button", { className: cx(mod("pl-btn", variant, size), className), ...rest }, children);
}
export function IconButton({ className, children, ...rest }) {
  return h("button", { className: cx("pl-iconbtn", className), ...rest }, children);
}
export function Card({ className, children, ...rest }) {
  return h("div", { className: cx("pl-card", className), ...rest }, children);
}
export function Panel({ className, children, ...rest }) {
  return h("div", { className: cx("pl-panel", className), ...rest }, children);
}
export function Badge({ variant, className, children, ...rest }) {
  return h("span", { className: cx(mod("pl-badge", variant), className), ...rest }, children);
}
export function Tag({ className, children, ...rest }) {
  return h("span", { className: cx("pl-tag", className), ...rest }, children);
}
export function Kbd({ className, children, ...rest }) {
  return h("kbd", { className: cx("pl-kbd", className), ...rest }, children);
}
export function Link({ className, children, ...rest }) {
  return h("a", { className: cx("pl-link", className), ...rest }, children);
}
export function Dot({ variant, pulse, className, ...rest }) {
  return h("span", { className: cx("pl-dot", variant && `pl-dot--${variant}`, pulse && "pl-dot--pulse", className), ...rest });
}
export function Divider({ className, ...rest }) {
  return h("hr", { className: cx("pl-divider", className), ...rest });
}
export function Spinner({ className, ...rest }) {
  return h("span", { className: cx("pl-spinner", className), role: "status", "aria-label": "loading", ...rest });
}
export function Skeleton({ className, ...rest }) {
  return h("div", { className: cx("pl-skel", className), ...rest });
}
export function Alert({ variant = "info", className, children, ...rest }) {
  return h("div", { role: "alert", className: cx(mod("pl-alert", variant), className), ...rest }, children);
}
export function Callout({ variant = "info", title, className, children, ...rest }) {
  return h("div", { className: cx(mod("pl-callout", variant), className), ...rest },
    title != null && h("div", { className: "pl-callout__title" }, title),
    h("div", { className: "pl-callout__body" }, children));
}
export function Tip({ className, children, ...rest }) {
  return h("div", { className: cx("pl-tip", className), ...rest }, children);
}

// ── Typography ──────────────────────────────────────────────────────────────

export function Heading({ as = "h2", className, children, ...rest }) {
  return h(as, { className: cx("pl-heading", className), ...rest }, children);
}
export function Eyebrow({ className, children, ...rest }) {
  return h("div", { className: cx("pl-eyebrow", className), ...rest }, children);
}
export function Lead({ className, children, ...rest }) {
  return h("p", { className: cx("pl-lead", className), ...rest }, children);
}
export function Prose({ className, children, ...rest }) {
  return h("div", { className: cx("pl-prose", className), ...rest }, children);
}

// ── Layout & structure ───────────────────────────────────────────────────────

export function Container({ className, children, ...rest }) {
  return h("div", { className: cx("pl-container", className), ...rest }, children);
}
export function Section({ className, children, ...rest }) {
  return h("section", { className: cx("pl-section", className), ...rest }, children);
}
export function Grid({ cols, gap, auto, className, children, style, ...rest }) {
  // `cols` = an explicit N-column grid; `auto` = responsive auto-fit; `gap` ∈ sm|md|lg.
  const st = cols ? { ...style, gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` } : style;
  return h("div", { className: cx("pl-grid", auto && "pl-grid--auto", gap && `pl-grid--gap-${gap}`, className), style: st, ...rest }, children);
}
export function Row({ label, desc, status, wide, className, children, ...rest }) {
  const cn = cx("pl-row", wide && "pl-row--wide", className);
  if (label == null && desc == null && status == null) return h("div", { className: cn, ...rest }, children);
  return h("div", { className: cn, ...rest },
    h("div", null,
      label != null && h("div", { className: "pl-row__label" }, label),
      desc != null && h("div", { className: "pl-row__desc" }, desc)),
    children,
    status != null && h("div", { className: "pl-row__status" }, status));
}
export function Hero({ cta, className, children, ...rest }) {
  return h("div", { className: cx("pl-hero", className), ...rest },
    children, cta && h("div", { className: "pl-hero__cta" }, cta));
}
export function Header({ name, org, actions, className, children, ...rest }) {
  return h("header", { className: cx("pl-header", className), ...rest },
    (name != null || org != null) && h("div", { className: "pl-header__brand" },
      name != null && h("div", { className: "pl-header__name" }, name),
      org != null && h("div", { className: "pl-header__org" }, org)),
    children,
    h("div", { className: "pl-header__spacer" }),
    actions && h("div", { className: "pl-header__actions" }, actions));
}
export function SideNav({ header, className, children, ...rest }) {
  return h("nav", { className: cx("pl-sidenav", className), ...rest },
    header && h("div", { className: "pl-sidenav__header" }, header), children);
}
export function SideNavItem({ icon, active, label, className, children, ...rest }) {
  return h("a", { className: cx("pl-sidenav__item", active && "pl-sidenav__item--active", className), ...rest },
    icon && h("span", { className: "pl-sidenav__icon" }, typeof icon === "string" ? h(Icon, { name: icon, size: 18 }) : icon),
    h("span", { className: "pl-sidenav__label" }, label ?? children));
}
export function AppShell({ header, sidebar, aside, className, children, ...rest }) {
  // A page frame: optional header row, a left sidebar column, a filling main column, and an
  // optional right aside. The console's AppShell is resizable; this is the static layout of it.
  return h("div", { className: cx("pl-appshell", className), ...rest },
    header && h("div", { className: "pl-appshell__header" }, header),
    sidebar && h("div", { className: "pl-appshell__col pl-appshell__col--left" }, sidebar),
    h("div", { className: "pl-appshell__col pl-appshell__col--fill" }, children),
    aside && h("div", { className: "pl-appshell__col pl-appshell__col--right" }, aside));
}

// ── Navigation ──────────────────────────────────────────────────────────────

export function Tabs({ segmented, className, children, ...rest }) {
  return h("div", { role: "tablist", className: cx("pl-tabs", segmented && "pl-tabs--segmented", className), ...rest }, children);
}
export function Tab({ active, icon, className, children, ...rest }) {
  return h("button", { role: "tab", "aria-selected": !!active, className: cx("pl-tab", active && "pl-tab--active", className), ...rest },
    icon && h("span", { className: "pl-tab__icon" }, typeof icon === "string" ? h(Icon, { name: icon, size: 16 }) : icon), children);
}
export function Segmented({ className, children, ...rest }) {
  return h("div", { role: "tablist", className: cx("pl-segmented", className), ...rest }, children);
}
export function SegmentedButton({ active, className, children, ...rest }) {
  return h("button", { "aria-selected": !!active, className: cx("pl-segmented__btn", active && "pl-segmented__btn--active", className), ...rest }, children);
}
export function Menu({ className, children, ...rest }) {
  return h("div", { role: "menu", className: cx("pl-menu", className), ...rest }, children);
}
export function MenuItem({ icon, destructive, label, className, children, ...rest }) {
  return h("button", { role: "menuitem", className: cx("pl-menu__item", destructive && "pl-menu__item--destructive", className), ...rest },
    icon && h("span", { className: "pl-menu__icon" }, typeof icon === "string" ? h(Icon, { name: icon, size: 16 }) : icon),
    h("span", { className: "pl-menu__label" }, label ?? children));
}
export function MenuSeparator({ className, ...rest }) {
  return h("div", { role: "separator", className: cx("pl-menu__sep", className), ...rest });
}

// ── Data & content ───────────────────────────────────────────────────────────

export function Stat({ value, label, className, ...rest }) {
  return h("div", { className: cx("pl-stat", className), ...rest },
    h("div", { className: "pl-stat__num" }, value),
    h("div", { className: "pl-stat__label" }, label));
}
export function Stats({ className, children, ...rest }) {
  return h("div", { className: cx("pl-stats", className), ...rest }, children);
}
export function Table({ className, children, ...rest }) {
  return h("table", { className: cx("pl-table", className), ...rest }, children);
}
export function Board({ className, children, ...rest }) {
  return h("div", { className: cx("pl-board", className), ...rest }, children);
}
export function Steps({ className, children, ...rest }) {
  return h("ol", { className: cx("pl-steps", className), ...rest }, children);
}
export function Step({ num, title, className, children, ...rest }) {
  return h("li", { className: cx("pl-step", className), ...rest },
    num != null && h("div", { className: "pl-step__num" }, num),
    h("div", { className: "pl-step__body" },
      title != null && h("div", { className: "pl-step__title" }, title), children));
}
export function Progress({ value = 0, max = 100, variant, caption, className, ...rest }) {
  const pct = max ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return h("div", { className: cx("pl-progress", className), ...rest },
    h("div", { className: "pl-progress__track" },
      h("div", { className: cx("pl-progress__fill", variant && `pl-progress__fill--${variant}`), style: { width: `${pct}%` } })),
    caption != null && h("div", { className: "pl-progress__caption" }, caption));
}
export function Accordion({ className, children, ...rest }) {
  return h("div", { className: cx("pl-accordion", className), ...rest }, children);
}
export function AccordionItem({ title, open, className, children, ...rest }) {
  return h("div", { className: cx("pl-accordion__item", open && "pl-accordion__item--open", className), ...rest },
    h("button", { className: "pl-accordion__trigger", "aria-expanded": !!open },
      h("span", { className: "pl-accordion__title" }, title),
      h("span", { className: "pl-accordion__chevron" }, h(Icon, { name: "chevron-down", size: 16 }))),
    h("div", { className: "pl-accordion__panel" }, children));
}
export function Avatar({ src, alt, className, children, ...rest }) {
  return h("span", { className: cx("pl-avatar", className), ...rest },
    src ? h("img", { className: "pl-avatar__img", src, alt: alt || "" }) : children);
}
export function Empty({ icon, title, desc, action, className, ...rest }) {
  return h("div", { className: cx("pl-empty", className), ...rest },
    icon && h("div", { className: "pl-empty__icon" }, typeof icon === "string" ? h(Icon, { name: icon, size: 32 }) : icon),
    title != null && h("div", { className: "pl-empty__title" }, title),
    desc != null && h("div", { className: "pl-empty__desc" }, desc),
    action && h("div", { className: "pl-empty__action" }, action));
}

// ── Overlays ────────────────────────────────────────────────────────────────

export function Dialog({ title, onClose, footer, className, children, ...rest }) {
  return h("div", { role: "dialog", "aria-modal": "true", className: cx("pl-dialog", className), ...rest },
    (title != null || onClose) && h("div", { className: "pl-dialog__head" },
      title != null && h("div", { className: "pl-dialog__title" }, title),
      onClose && h("button", { className: "pl-dialog__close", onClick: onClose, "aria-label": "Close" }, h(Icon, { name: "x", size: 18 }))),
    h("div", { className: "pl-dialog__body" }, children),
    footer && h("div", { className: "pl-dialog__foot" }, footer));
}
export function Drawer({ side = "right", title, footer, className, children, ...rest }) {
  return h("div", { role: "dialog", className: cx("pl-drawer", `pl-drawer--${side}`, className), ...rest },
    title != null && h("div", { className: "pl-drawer__head" }, h("div", { className: "pl-drawer__title" }, title)),
    h("div", { className: "pl-drawer__body" }, children),
    footer && h("div", { className: "pl-drawer__foot" }, footer));
}

// ── Forms ───────────────────────────────────────────────────────────────────

export function Input({ className, ...rest }) {
  return h("input", { className: cx("pl-input", className), ...rest });
}
export function Textarea({ className, ...rest }) {
  return h("textarea", { className: cx("pl-textarea", className), ...rest });
}
export function Select({ className, children, ...rest }) {
  return h("select", { className: cx("pl-select", className), ...rest }, children);
}
export function Field({ label, hint, className, children, ...rest }) {
  return h("label", { className: cx("pl-field", className), ...rest },
    label != null && h("span", { className: "pl-field__label" }, label),
    h("span", { className: "pl-field__input" }, children),
    hint != null && h("span", { className: "pl-field__hint" }, hint));
}
export function Switch({ label, disabled, className, ...rest }) {
  return h("label", { className: cx("pl-switch", disabled && "pl-switch--disabled", className) },
    h("input", { type: "checkbox", className: "pl-switch__input", disabled, ...rest }),
    h("span", { className: "pl-switch__track" }, h("span", { className: "pl-switch__thumb" })),
    label != null && h("span", { className: "pl-switch__label" }, label));
}
export function Checkbox({ label, disabled, className, ...rest }) {
  return h("label", { className: cx("pl-checkbox", disabled && "pl-checkbox--disabled", className) },
    h("input", { type: "checkbox", className: "pl-checkbox__input", disabled, ...rest }),
    h("span", { className: "pl-checkbox__box" }),
    label != null && h("span", { className: "pl-checkbox__label" }, label));
}

// ── Icon (lucide, vendored) ───────────────────────────────────────────────────

const pascal = (n) => String(n).replace(/(^|[-_\s])([a-zA-Z])/g, (_, __, c) => c.toUpperCase());
export function Icon({ name, size = 24, color = "currentColor", strokeWidth = 2, className, ...rest }) {
  const node = lucideIcons[pascal(name)];
  if (!node) return null;
  return h("svg", {
    xmlns: "http://www.w3.org/2000/svg", width: size, height: size, viewBox: "0 0 24 24",
    fill: "none", stroke: color, strokeWidth, strokeLinecap: "round", strokeLinejoin: "round",
    className: cx("pl-icon", className), "aria-hidden": "true", ...rest,
  }, node.map(([tag, attrs], i) => h(tag, { key: i, ...attrs })));
}
