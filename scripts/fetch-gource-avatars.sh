#!/usr/bin/env bash
# Fetch GitHub avatars for gource visualization
# Uses git log emails to find GitHub usernames, then downloads profile pictures
set -eo pipefail

AVATAR_DIR="${1:-.gource/avatars}"
mkdir -p "$AVATAR_DIR"

echo "Fetching GitHub avatars into $AVATAR_DIR..."

# Tab-separated: "Git Name<TAB>GitHub username"
USERS="Josh Mabry	mabry1985
Josh	mabry1985
Dennis F	RomeoRaven
Matt Preston	gnostichumor
Claude	anthropics
GitHub CI	actions
github-actions[bot]	actions"

downloaded=0
skipped=0
failed=0

while IFS=$'\t' read -r name username; do
  output="$AVATAR_DIR/${name}.png"

  if [[ -f "$output" ]]; then
    skipped=$((skipped + 1))
    continue
  fi

  url="https://github.com/${username}.png?size=256"
  http_code=$(curl -sL -o "$output" -w "%{http_code}" "$url")
  if [[ "$http_code" == "200" ]]; then
    downloaded=$((downloaded + 1))
    echo "  + $name ($username)"
  else
    rm -f "$output"
    failed=$((failed + 1))
    echo "  x $name ($username) — HTTP $http_code"
  fi

  sleep 0.2
done <<< "$USERS"

echo ""
echo "Done: $downloaded downloaded, $skipped cached, $failed failed"
echo "Avatars in: $AVATAR_DIR"
