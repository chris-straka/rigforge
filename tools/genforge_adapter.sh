#!/bin/bash
# genforge adapter entry (repair-rig): syncs the addon overlay and runs
# tools/genforge_adapter.py in headless Blender. Contract and exit codes
# are documented there:
#   tools/genforge_adapter.sh repair-rig IN.glb OUT.glb RESULT.json [--class humanoid|quadruped|custom]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BLENDER="${BLENDER_BIN:-/Applications/Blender.app/Contents/MacOS/Blender}"
# Per-user overlay; RIGFORGE_SCRIPTS overrides it (parallel callers).
SCRIPTS="${RIGFORGE_SCRIPTS:-/tmp/rf_scripts}"

if [ ! -x "$BLENDER" ]; then
    echo "genforge_adapter.sh: Blender not found at $BLENDER (set BLENDER_BIN)" >&2
    exit 2
fi

mkdir -p "$SCRIPTS/addons"
rsync -a --delete "$ROOT/rigforge/" "$SCRIPTS/addons/rigforge/"
exec env BLENDER_USER_SCRIPTS="$SCRIPTS" "$BLENDER" \
    --background --factory-startup \
    --python-exit-code 2 \
    --python "$ROOT/tools/genforge_adapter.py" -- "$@"
