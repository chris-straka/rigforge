# SPDX-License-Identifier: GPL-2.0-or-later
"""Validation gate CLI (thin shim).

Implementation lives in rigforge.auto_place.validate_fit (shared with
wm.rigforge_auto_place); this file preserves the headless CLI contract
(argv, markers, exit codes) and re-exports the module for in-session
load_tool/runpy callers (tests/test_auto_placement.py, tools/auto_rig.py).

Headless (from the repo root; needs the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/validate_fit.py -- \\
      MESH FITTED_METARIG.py LANDMARKS.json [--margin M] [--render DIR]
      [--no-render] [--on-fail stop|preset] [--open bone,...] [--report PATH]
      [--closeup PNG]
"""

from rigforge.auto_place.validate_fit import *  # noqa: F403
from rigforge.auto_place.validate_fit import main

if __name__ == "__main__":
    main()
