- **Vega-lite chart artifacts now theme from the design system, not a hand-rolled copy (#4171).**
  The artifact shell's chart frame built its Vega config and picked its tooltip theme from a
  `theme()`/`isDark()` pair hand-written inside `artVega`. Those are gone: the frame now draws
  from `@protolabsai/design`'s pure `buildVegaConfig(tok)` / `vegaTooltipTheme(tok)`, vendored
  verbatim beside the other chart libs and injected into the nonce-CSP frame as source. Every
  consumer themes identically from one source, and a drift test keeps the vendored copy in step
  with the package. Visible changes: legends move to the bottom (horizontal, four columns, no
  symbol cap, so 8+ series wrap), axis/legend labels get more room, and sequential / heatmap /
  diverging ranges are emitted as hex — so heat maps use the console palette instead of Vega's
  default blues. The loader deny, `ast:true` interpreter, `prep()` autosize, and live re-theme
  are unchanged.
