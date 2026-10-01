# SPDX-License-Identifier: GPL-2.0-or-later
"""Landmark detector CLI (thin shim).

Implementation lives in rigforge.auto_place.detect_landmarks (shared with
wm.rigforge_auto_place); this file preserves the headless CLI contract
(argv, markers, exit codes) and re-exports the module for in-session
load_tool/runpy callers (tests/test_auto_placement.py, tools/auto_rig.py).

Headless (from the repo root; needs the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/detect_landmarks.py -- \\
      MESH [--kind biped|quadruped] [--facing -Y|+Y] [--check]
"""

from rigforge.auto_place.detect_landmarks import *  # noqa: F403
from rigforge.auto_place.detect_landmarks import main

if __name__ == "__main__":
    main()
