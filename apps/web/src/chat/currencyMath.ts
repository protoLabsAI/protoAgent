// Currency-vs-math disambiguation for assistant markdown.
//
// The DS `<Markdown>` (`@protolabsai/ui/markdown`) wires `remark-math` with its default
// options, which enable the single-dollar inline-math delimiter `$…$`. That's correct for
// math, but it also means a reply with two currency amounts in one paragraph —
// "~A$180M total raised … A$63–90M" — gets everything BETWEEN the dollar signs parsed as an
// inline-math node and rendered in KaTeX's serif-italic font. It reads as "random LaTeX"
// because it only fires when the `$`s happen to pair up. We can't fix this via the DS's
// `remarkPlugins` prop — that only APPENDS, so the DS's already-registered `remark-math`
// (single-dollar on) still tokenizes first. So we pre-empt the tokenizer at the string level.
//
// The heuristic: a `$` immediately followed by a digit is currency, not math — LLMs write
// `$180M` / `$600M`, whereas inline math opens on a letter or backslash (`$x^2$`, `$\pi$`).
// Escaping just those `$`s to `\$` (which CommonMark renders as a literal `$`, backslash
// consumed) neutralizes the currency false-positive while leaving BOTH real forms intact:
// single-`$` inline math (`$x^2$`) AND display math (`$$…$$`). This is deliberately narrower
// than the DS-wide `singleDollarTextMath: false`, which would kill legit `$x^2$` too.
//
// The regex: match a lone `$` that is
//   (?<![\\$])  — not already escaped (`\$`) and not the 2nd char of a `$$` display fence
//   \$          — the dollar sign
//   (?=\d)      — immediately followed by a digit (the currency tell)
// The first `$` of a `$$5…` fence is followed by `$` (not a digit) so it never matches, and
// the second is preceded by `$` so the lookbehind excludes it — display math survives.
//
// Only collateral: genuine inline math that OPENS on a digit (`$2^n$`) is escaped. That's
// rare next to how often currency appears in agent output, and the trade was chosen
// deliberately (see the fix discussion / DS gap on protoContent).
const CURRENCY_DOLLAR = /(?<![\\$])\$(?=\d)/g;

/** Escape currency-style `$` (a `$` directly before a digit) so `remark-math` doesn't parse
 *  it as an inline-math delimiter. Identity for real math (`$x^2$`, `$$…$$`) and for `$`
 *  not followed by a digit. */
export function escapeCurrencyDollars(markdown: string): string {
  return markdown.replace(CURRENCY_DOLLAR, "\\$");
}
