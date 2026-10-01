# SPDX-License-Identifier: GPL-2.0-or-later
"""unirig-joints/1 hints CLI (thin shim).

Implementation lives in rigforge.auto_place.unirig_hints (shared with
the fitter and wm.rigforge_auto_place); this file preserves the headless
CLI contract and re-exports the module for in-session load_tool callers
(tests/test_auto_placement.py).

Headless (from the repo root; needs the addon overlay):
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup \\
      --python tools/unirig_hints.py -- HINTS.json LANDMARKS.json [--rotated]
"""

from rigforge.auto_place.unirig_hints import *  # noqa: F403
from rigforge.auto_place.unirig_hints import main

if __name__ == "__main__":
    main()
