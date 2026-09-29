- **Console: chat.css and hitl.css radius + off-scale spacing now read DS tokens (protoContent#525/#547).**
  Every `border-radius` px literal in `apps/web/src/chat/chat.css` and `chat/hitl.css` moves onto the DS
  radius scale (`--pl-radius`/`-md`/`-lg`/`-pill`) — dots/chips/badges → `-pill`, the 8–12px in-flow cards
  (`.chat-scheduled-card`, `.chat-server-result-card`, `.chat-delegation-row`) → `-lg`, 6px → `-md`, and
  2–5px → `--pl-radius` — and every padding/margin/gap/inset/offset px moves onto the spacing scale,
  including the half-steps the DS shipped on `@protolabsai/design` 0.11.0 (`--pl-space-0_5/-1_5/-2_5`).
  Off-scale 3/5/7/14px values snap to the nearest DS step (rounding down with the local rhythm); 1px
  hairlines, border/outline widths, font/icon sizes and `50%`/`0` radii are left alone. Visual-only token
  substitution — the token values equal the literals they replace.
