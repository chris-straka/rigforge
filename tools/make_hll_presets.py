# SPDX-License-Identifier: GPL-2.0-or-later
"""HLL preset builder CLI (thin shim).

Implementation lives in rigforge.auto_place.make_hll_presets (its
descendants/apply/about/reconnect_from machinery is shared with the
fitter and wm.rigforge_auto_place); this file preserves the headless
CLI contract and re-exports the module for in-session load_tool callers.

Headless (from the repo root; needs the addon overlay):
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup \\
      --python tools/make_hll_presets.py -- .
"""

from rigforge.auto_place.make_hll_presets import *  # noqa: F403
from rigforge.auto_place.make_hll_presets import main

if __name__ == "__main__":
    main()
