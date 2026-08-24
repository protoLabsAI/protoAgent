#!/usr/bin/env bash
# Render a ~30-second gource visualization of protoAgent
# Starting from the first commit (2026-04-17) through present
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

OUTPUT="${1:-gource.mp4}"
AVATAR_DIR=".gource/avatars"
START_DATE="2026-04-17"
TARGET_DURATION=30

# Calculate seconds-per-day for target duration
days_since_start=$(python3 -c "
from datetime import date
d = (date.today() - date.fromisoformat('$START_DATE')).days
print(d)
")
spd=$(python3 -c "print(round($TARGET_DURATION / $days_since_start, 2))")

# PR count for the title — squash-merged commits carry "(#NNNN)"
pr_count=$(git log --oneline | grep -cE '\(#[0-9]+\)')

echo "=== protoAgent Gource Render ==="
echo "Start date:   $START_DATE"
echo "Days elapsed: $days_since_start"
echo "PRs merged:   ~$pr_count"
echo "Target:       ${TARGET_DURATION}s video"
echo "Speed:        ${spd}s per day"
echo "Output:       $OUTPUT"
echo ""

# Fetch avatars if directory is empty or missing
if [[ ! -d "$AVATAR_DIR" ]] || [[ -z "$(ls -A "$AVATAR_DIR" 2>/dev/null)" ]]; then
  echo "Fetching GitHub avatars..."
  bash scripts/fetch-gource-avatars.sh "$AVATAR_DIR"
  echo ""
fi

avatar_count=$(ls -1 "$AVATAR_DIR"/*.png 2>/dev/null | wc -l | tr -d ' ')
echo "Using $avatar_count avatars from $AVATAR_DIR"
echo "Rendering..."
echo ""

gource \
  --start-date "$START_DATE" \
  --seconds-per-day "$spd" \
  --auto-skip-seconds 0.5 \
  --stop-at-end \
  --disable-input \
  --disable-auto-rotate \
  --max-file-lag 0.8 \
  --elasticity 0.01 \
  --time-scale 0.5 \
  --max-user-speed 200 \
  --user-friction 1.0 \
  --title "protoAgent — $pr_count PRs in $days_since_start Days" \
  --key \
  --multi-sampling \
  --bloom-multiplier 1.2 \
  --bloom-intensity 0.4 \
  --camera-mode overview \
  --padding 1.15 \
  --user-scale 1.5 \
  --user-image-dir "$AVATAR_DIR" \
  --highlight-users \
  --highlight-colour 9B87F2 \
  --font-colour FFFFFF \
  --date-format "%b %d" \
  --hide mouse,filenames \
  --dir-name-depth 2 \
  --file-idle-time 3 \
  --max-files 0 \
  --background-colour 111111 \
  --font-size 18 \
  --file-filter "package-lock|uv\.lock|\.beads/" \
  --output-ppm-stream - \
  --output-framerate 60 \
  -1920x1080 \
  | ffmpeg -y -r 60 -f image2pipe -vcodec ppm -i - \
  -vcodec libx264 -preset medium -pix_fmt yuv420p \
  -crf 18 -movflags +faststart \
  "$OUTPUT"

echo ""
echo "Done! Output: $OUTPUT"
ls -lh "$OUTPUT"
