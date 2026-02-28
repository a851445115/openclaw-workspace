#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/Users/chengren17/cross-platform-evolution"
export PREFLIGHT_CONFIG="${PREFLIGHT_CONFIG:-$REPO_DIR/config/preflight-router.json}"

exec "$REPO_DIR/scripts/run_preflight.sh" "$@"
