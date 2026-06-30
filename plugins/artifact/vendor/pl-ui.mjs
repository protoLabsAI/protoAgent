// @pl/ui — thin authored React wrappers over the protoLabs design-system `.pl-*` classes
// (the DS ships .tsx source, not browser ESM, so we can't import its real components into
// the sandbox; these mirror the class contracts and share the artifact's React instance via
// the import map). Pair with the injected plugin-kit.css so they pick up the live theme.
// Icons are backed by the vendored lucide icon data — no separate icon dependency.
import React from "react";
import { icons as lucideIcons } from "lucide";
const h = React.createElement;
const cx = (...a) => a.filter(Boolean).join(" ");
const mod = (base, variant, size) => cx(base, variant && `${base}--${variant}`, size && `${base}--${size}`);

export function Button({ variant, size, className, children, ...rest }) {
  return h("button", { className: cx(mod("pl-btn", variant, size), className), ...rest }, children);
}
export function Card({ className, children, ...rest }) {
  return h("div", { className: cx("pl-card", className), ...rest }, children);
}
export function Badge({ variant, className, children, ...rest }) {
  return h("span", { className: cx(mod("pl-badge", variant), className), ...rest }, children);
}
export function Alert({ variant = "info", className, children, ...rest }) {
  return h("div", { role: "alert", className: cx(mod("pl-alert", variant), className), ...rest }, children);
}
export function Tag({ className, children, ...rest }) {
  return h("span", { className: cx("pl-tag", className), ...rest }, children);
}
export function Kbd({ className, children, ...rest }) {
  return h("kbd", { className: cx("pl-kbd", className), ...rest }, children);
}
export function Input({ className, ...rest }) {
  return h("input", { className: cx("pl-input", className), ...rest });
}
export function Stat({ value, label, className, ...rest }) {
  return h("div", { className: cx("pl-stat", className), ...rest },
    h("div", { className: "pl-stat__num" }, value),
    h("div", { className: "pl-stat__label" }, label));
}

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
