- **Dropped the dead shadcn/Tailwind CSS wiring from the operator console (#3686).**
  ADR 0037's incremental migration left `app/tailwind.css` (the `@tailwind` layers plus a
  shadcn `:root` token block) and `tailwind.config.cjs` behind, but nothing consumed them —
  no Tailwind utility classNames, no `@apply`, and preflight was already off. The stylesheet,
  its config and the `main.tsx` import are removed; `postcss.config.cjs` is reduced to
  autoprefixer only (Tailwind is gone, but autoprefixer still supplies the `-moz-user-select`
  and other vendor prefixes the source relies on). The built CSS loses only the 65 unused
  `--tw-*` custom properties and the shadcn `:root` block — every colour already resolves
  through the `--pl-*` design tokens, so nothing renders differently. (Part 1 of 2; part 2
  removes the now-unused npm deps and `components.json`.)
