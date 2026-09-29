- **Console: the archetype picker's "What's included" control is now a DS Button (#3832, protoContent#551).**
  The per-card `What's included →` action in `ArchetypePicker` moved off the hand-rolled
  `.archetype-preview-link` button onto the design-system `Button` (variant `ghost`, size
  `sm`), satisfying the DS action-button rule. Its aria-label and preview handler are
  unchanged; the old `.archetype-preview-link` CSS is gone, leaving only a layout-only
  start-alignment rule for the card's button.
