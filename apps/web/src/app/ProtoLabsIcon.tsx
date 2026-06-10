/**
 * The protoLabs.studio bot mark, from protoContent's brand assets
 * (docs/assets/brand/protolabs-icon{,-outline}.svg). Ported from ORBIS's
 * `ProtoLabsIcon` so the loading screens render a crisp inline SVG instead of
 * a static <img> — and so the mark can be recolored to the app's lavender
 * chrome accent (#9b87f2) rather than the brand-default violet (#7c3aed),
 * which is muddy on the dark background.
 *
 * Per brand rules the mark itself is never deformed; only the icon background
 * may be recolored.
 *
 * - `flat` (default): lavender rounded square + white robot — the app/brand
 *   icon at moderate sizes.
 * - `outline`: face-only lavender strokes on transparent — for inline-with-
 *   text / no-compete contexts (what the launch splash + boot gate use).
 * - `white`: white robot, no background — for dark chrome (e.g. a title bar).
 */
export function ProtoLabsIcon({
  size = 64,
  variant = "flat",
  className,
  decorative = false,
  gradientStroke = false,
}: {
  size?: number;
  variant?: "flat" | "outline" | "white";
  className?: string;
  /** When true the SVG is hidden from a11y (the labelled container carries the
   *  name) — avoids a redundant nested "protoLabs.studio" announcement. */
  decorative?: boolean;
  /** Stroke the (outline) mark with the brand gradient instead of solid lavender,
   *  matching the gradient wordmark. References `#pl-brand-gradient` — the def the
   *  DS Splash renders (or mount <BrandGradientDef/>). CSS can't gradient a slotted
   *  SVG from outside, so the fill must live on the SVG itself. */
  gradientStroke?: boolean;
}) {
  const robotStroke = gradientStroke
    ? "url(#pl-brand-gradient)"
    : variant === "outline"
      ? "#9b87f2"
      : "#ffffff";
  const a11y = decorative
    ? { "aria-hidden": true as const }
    : { role: "img", "aria-label": "protoLabs.studio" };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 256 256"
      className={className}
      {...a11y}
    >
      {variant === "flat" && (
        <rect x="16" y="16" width="224" height="224" rx="56" fill="#9b87f2" />
      )}
      <g
        transform="translate(224, 32) scale(-8, 8)"
        fill="none"
        stroke={robotStroke}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M12 8V4H8" />
        <rect width="16" height="12" x="4" y="8" rx="2" />
        <path d="M2 14h2" />
        <path d="M20 14h2" />
        <path d="M15 13v2" />
        <path d="M9 13v2" />
      </g>
    </svg>
  );
}
