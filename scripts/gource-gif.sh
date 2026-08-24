#!/usr/bin/env bash
# Convert the gource render (or any mp4) to a shareable GIF.
# Two-pass ffmpeg palettegen/paletteuse for clean colors on the dark background.
#
# Usage: gource-gif.sh [input.mp4] [output.gif] [width] [fps]
set -euo pipefail

INPUT="${1:-gource.mp4}"
OUTPUT="${2:-${INPUT%.mp4}.gif}"
WIDTH="${3:-640}"
FPS="${4:-15}"

if [[ ! -f "$INPUT" ]]; then
  echo "Input not found: $INPUT" >&2
  echo "Render it first: bash scripts/render-gource.sh" >&2
  exit 1
fi

PALETTE_DIR="$(mktemp -d -t gource-gif)"
PALETTE="$PALETTE_DIR/palette.png"
trap 'rm -rf "$PALETTE_DIR"' EXIT

FILTERS="fps=$FPS,scale=$WIDTH:-1:flags=lanczos"

echo "=== GIF conversion ==="
echo "Input:  $INPUT"
echo "Output: $OUTPUT (${WIDTH}px wide, ${FPS}fps)"
echo ""

ffmpeg -y -v warning -i "$INPUT" \
  -vf "$FILTERS,palettegen=stats_mode=diff" \
  -update 1 -frames:v 1 "$PALETTE"

ffmpeg -y -v warning -i "$INPUT" -i "$PALETTE" \
  -lavfi "$FILTERS [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  "$OUTPUT"

echo "Done! Output: $OUTPUT"
ls -lh "$OUTPUT"
