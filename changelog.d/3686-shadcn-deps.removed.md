- **Removed the now-unused shadcn/Tailwind npm deps and `components.json` from the console (#3686).**
  With part 1's Tailwind CSS wiring gone, nothing imports `@radix-ui/react-dropdown-menu`,
  `class-variance-authority`, `clsx`, `react-markdown`, `rehype-highlight`, `remark-gfm`,
  `tailwind-merge`, `tailwindcss-animate` or `tailwindcss` — all are dropped from
  `apps/web/package.json`, and the stale shadcn config (`components.json`, which pointed at a
  `src/components/ui` and `src/lib/cn` that never existed) is deleted. `autoprefixer` and
  `postcss` stay, since part 1 left an autoprefixer-only `postcss.config.cjs`. The markdown /
  code-block / graph stack (streamdown, katex, shiki, `@xyflow/react`, the DS packages) is
  untouched. (Part 2 of 2, closes the issue.)
