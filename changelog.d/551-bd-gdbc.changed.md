- **Console: the MCP catalog "All servers" back control is now the DS `Button` (#3832, protoContent#551).**
  The action-button rule (design-system card 3a) moves the hand-rolled
  `<button className="mcp-catalog-back">` in the Add-a-common-MCP-server dialog to
  `<Button variant="ghost" size="sm">` with the same click behavior and children, and
  drops the now-unused `.mcp-catalog-back` rule from `theme.css`. The UpdateNotice update
  pill is left as-is: its floating look (999px pill radius, raised background, popover
  shadow) has no DS `Button` prop or token equivalent, and reproducing it would mean
  hand-rolled overrides the rule is meant to remove.
