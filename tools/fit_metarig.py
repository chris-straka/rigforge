# SPDX-License-Identifier: GPL-2.0-or-later
"""Metarig fitter CLI (thin shim).

Implementation lives in rigforge.auto_place.fit_metarig (shared with
wm.rigforge_auto_place); this file preserves the headless CLI contract
(argv, markers, exit codes) and re-exports the module for in-session
load_tool/runpy callers (tests/test_auto_placement.py, tools/auto_rig.py).

Headless (from the repo root; needs the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/fit_metarig.py -- \\
      MESH LANDMARKS.json [--out FILE] [--blend FILE] [--no-generate]
      [--preset hll_hero|hll_stalker] [--hints HINTS.json] [--hints-rotated]
"""

from rigforge.auto_place.fit_metarig import *  # noqa: F403
from rigforge.auto_place.fit_metarig import main

if __name__ == "__main__":
    main()
