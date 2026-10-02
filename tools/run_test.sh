#!/bin/bash
# Headless test runner: syncs the addon overlay and runs one tests/*.py
# inside Blender. Usage: tools/run_test.sh tests/test_foo.py
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BLENDER="${BLENDER_BIN:-/Applications/Blender.app/Contents/MacOS/Blender}"
SCRIPTS=/tmp/rf_scripts

if [ ! -x "$BLENDER" ]; then
    echo "run_test.sh: Blender not found at $BLENDER (set BLENDER_BIN)" >&2
    exit 1
fi
if [ $# -lt 1 ]; then
    echo "usage: tools/run_test.sh tests/test_foo.py" >&2
    exit 2
fi

case "$1" in
    /*) TEST="$1" ;;
    *) TEST="$ROOT/$1" ;;
esac

mkdir -p "$SCRIPTS/addons"
rsync -a --delete "$ROOT/rigforge/" "$SCRIPTS/addons/rigforge/"
exec env BLENDER_USER_SCRIPTS="$SCRIPTS" "$BLENDER" \
    --background --factory-startup --python "$TEST"
