/** @type {import('tailwindcss').Config} */
// ADR 0037 — the console's Tailwind config. Pulls the protoLabs brand tokens via the
// @protolabsai/design preset, and adds the shadcn color convention (mapped to those tokens)
// so shadcn/Radix components render on-brand. Preflight is OFF so Tailwind coexists with the
// legacy theme.css without resetting base styles (incremental migration, ADR 0037 D4).
module.exports = {
  presets: [require("@protolabsai/design/tailwind")],
  // (The streamdown dist scan is gone — the DS `<Markdown>` owns markdown chrome styling
  // via its self-contained markdown.css; streamdown's Tailwind classes are inert here.)
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        border: "var(--border)",
        input: "var(--input)",
        ring: "var(--ring)",
        primary: { DEFAULT: "var(--primary)", foreground: "var(--primary-foreground)" },
        secondary: { DEFAULT: "var(--secondary)", foreground: "var(--secondary-foreground)" },
        muted: { DEFAULT: "var(--muted)", foreground: "var(--muted-foreground)" },
        accent: { DEFAULT: "var(--accent)", foreground: "var(--accent-foreground)" },
        destructive: { DEFAULT: "var(--destructive)", foreground: "var(--destructive-foreground)" },
        popover: { DEFAULT: "var(--popover)", foreground: "var(--popover-foreground)" },
        card: { DEFAULT: "var(--card)", foreground: "var(--card-foreground)" },
      },
      borderRadius: { lg: "var(--radius)", md: "calc(var(--radius) - 2px)", sm: "calc(var(--radius) - 4px)" },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
