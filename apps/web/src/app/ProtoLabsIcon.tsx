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
import { useId } from "react";

export function ProtoLabsIcon({
  size = 64,
  variant = "flat",
  className,
  decorative = false,
  gradientStroke = false,
  tone = "lavender",
}: {
  size?: number;
  variant?: "flat" | "outline" | "white";
  className?: string;
  /** When true the SVG is hidden from a11y (the labelled container carries the
   *  name) — avoids a redundant nested "protoLabs.studio" announcement. */
  decorative?: boolean;
  /** Stroke the (outline) mark with the brand gradient instead of solid lavender,
   *  matching the gradient wordmark. The gradient def lives ON this SVG (below) so
   *  it's self-contained — and it MUST be `userSpaceOnUse`: an objectBoundingBox
   *  gradient (e.g. the DS `#pl-brand-gradient`) can't paint the mark's eyes/ears,
   *  which are axis-aligned strokes with a zero-area bbox — Chrome/WebKit drop them
   *  entirely. (We used to reference `#pl-brand-gradient`, which is exactly why the
   *  Splash logo lost its eyes + ears.) */
  gradientStroke?: boolean;
  /** "lavender" (default) = the fixed brand-chrome lavender. "accent" = follow the
   *  live theme accent (`--pl-color-accent`) so the mark recolors with the operator's
   *  chosen accent — the `flat` background square or the `outline` strokes. Routed
   *  through `currentColor` because an SVG presentation attribute can't hold `var()`. */
  tone?: "lavender" | "accent";
}) {
  // Unique, selector-safe id per instance (useId yields ":r0:"-style ids; strip
  // the colons so the funciri url(#…) and any tooling stay happy).
  const gradId = `pl-grad-${useId().replace(/:/g, "")}`;
  // The recolorable lavender chrome. "accent" paints it with the live theme accent via
  // currentColor (set on the <svg> below); brand rules still hold — the robot in `flat`
  // stays white, only the background square / outline strokes take the accent.
  const lavender = tone === "accent" ? "currentColor" : "#9b87f2";
  const robotStroke = gradientStroke
    ? `url(#${gradId})`
    : variant === "outline"
      ? lavender
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
      style={tone === "accent" ? { color: "var(--pl-color-accent, #9b87f2)" } : undefined}
      {...a11y}
    >
      {gradientStroke && (
        <defs>
          {/* userSpaceOnUse + coords in the mark's LOCAL space (the paths' own
              coordinate system, inside the translate/scale group) — paints every
              subpath including the zero-area eye/ear strokes. "lavender" tone is the
              fixed brand gradient (lavender → violet); "accent" tone derives a light→dark
              gradient from the live theme accent so it white-labels and coheres with the
              DS splash/bootgate neon glow (already keyed to --pl-color-accent). The accent
              stops use `style` (CSS context) because var()/color-mix() can't resolve in an
              SVG stop-color presentation attribute. */}
          <linearGradient id={gradId} gradientUnits="userSpaceOnUse" x1="2" y1="4" x2="22" y2="20">
            {tone === "accent" ? (
              <>
                <stop offset="0" style={{ stopColor: "color-mix(in srgb, var(--pl-color-accent, #9b87f2), #fff 22%)" }} />
                <stop offset="1" style={{ stopColor: "color-mix(in srgb, var(--pl-color-accent, #7c3aed), #000 25%)" }} />
              </>
            ) : (
              <>
                <stop offset="0" stopColor="#9b87f2" />
                <stop offset="1" stopColor="#7c3aed" />
              </>
            )}
          </linearGradient>
        </defs>
      )}
      {variant === "flat" && (
        <rect x="16" y="16" width="224" height="224" rx="56" fill={lavender} />
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
