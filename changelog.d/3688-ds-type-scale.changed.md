- **Console: bumped `@protolabsai/design` to ^0.10.0 and `@protolabsai/ui` to ^0.62.1 for the DS type scale (#3688).**
  The design package now ships the `--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl}` custom
  properties (10/11/12/13/14/16/18px) that the console's font-size migration will move
  sites onto. This card is dependency-only — no CSS or component source changes — and lays
  the foundation the later type-scale cards build on.
