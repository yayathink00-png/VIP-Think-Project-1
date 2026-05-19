#!/usr/bin/env bash
set -euo pipefail

# Opens Jimeng in a dedicated Chrome profile so Codex/browser automation does
# not touch the user's daily browser session.

PROFILE_DIR="${JIMENG_CHROME_PROFILE_DIR:-$HOME/.codex/jimeng-chrome-profile}"
WORKSPACE_URL="${1:-https://jimeng.jianying.com/ai-tool/generate?type=video&workspace=13101107985676}"
CHROME_APP_NAME="${CHROME_APP_NAME:-Google Chrome}"
EXTENSION_DIR="${OPENCLI_EXTENSION_DIR:-$(cd "$(dirname "$0")/.." && pwd)/tools/opencli/opencli-extension-v1.0.15}"

if [[ ! -d "/Applications/${CHROME_APP_NAME}.app" ]]; then
  echo "ERROR: Chrome app not found: /Applications/${CHROME_APP_NAME}.app" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

ARGS=(
  --user-data-dir="$PROFILE_DIR" \
  --profile-directory="JimengAutomation" \
  --new-window \
  --no-first-run \
  --disable-default-apps \
)

if [[ -d "$EXTENSION_DIR" ]]; then
  ARGS+=(--load-extension="$EXTENSION_DIR")
fi

ARGS+=("$WORKSPACE_URL")

open -na "$CHROME_APP_NAME" --args "${ARGS[@]}"
