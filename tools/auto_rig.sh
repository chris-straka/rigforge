#!/bin/bash
# One-command entry to the rigforge auto-rig pipeline.
#
# For external harnesses (e.g. unirig-mac eval triage) that can only run a
# single executable with no shell features: this wraps the addon overlay
# sync + headless Blender invocation. CLI contract lives in
# tools/auto_rig.py: MESH --preset hll_hero|hll_stalker --out FILE ...
# Exit 0 + GLB on success; exit 1 naming stage+reason on failure.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BLENDER="${BLENDER_BIN:-/Applications/Blender.app/Contents/MacOS/Blender}"
SCRIPTS=/tmp/rf_scripts

if [ ! -x "$BLENDER" ]; then
    echo "auto_rig.sh: Blender not found at $BLENDER (set BLENDER_BIN)" >&2
    exit 1
fi

mkdir -p "$SCRIPTS/addons"
rsync -a --delete "$ROOT/rigforge/" "$SCRIPTS/addons/rigforge/"
exec env BLENDER_USER_SCRIPTS="$SCRIPTS" "$BLENDER" \
    --background --factory-startup \
    --python "$ROOT/tools/auto_rig.py" -- "$@"
