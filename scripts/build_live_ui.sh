#!/usr/bin/env bash
# Build the live-voice React app so FastAPI's /live mount can serve it.
# Used by Nixpacks (see nixpacks.toml) and available for local builds.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/web/live-voice"

if [ -d node_modules ] && [ -f package-lock.json ] && [ node_modules -nt package-lock.json ]; then
  echo "[build_live_ui] node_modules up to date, skipping install"
else
  npm ci --no-audit --no-fund
fi

npm run build
echo "[build_live_ui] built -> $repo_root/web/live-voice/dist"
