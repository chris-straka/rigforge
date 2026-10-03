# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run one headless test under the `bpy` pip wheel (no Blender install).

    python tools/run_test_bpy.py tests/test_cleanup.py

Same overlay as tools/run_test.sh (addon copied to $RF_SCRIPTS/addons,
default /tmp/rf_scripts), then factory settings and the test script,
in-process. Needs the wheel's Python: `pip install bpy` (5.0.1 wants
CPython 3.11; Linux also needs libegl1). The wheel trails the Blender
this fork tracks (5.2), so the Mac binary stays canonical; this is
the cross-check for machines without Blender (cloud sessions, CI).
"""

import os
import runpy
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python tools/run_test_bpy.py tests/test_foo.py [args]")
    test = os.path.abspath(sys.argv[1])
    scripts = os.environ.get("RF_SCRIPTS", "/tmp/rf_scripts")
    dst = os.path.join(scripts, "addons", "rigforge")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "rigforge"), dst)
    os.environ["BLENDER_USER_SCRIPTS"] = scripts
    os.chdir(ROOT)  # tests put "tests" on sys.path
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=False)
    bpy.utils.refresh_script_paths()
    sys.argv = ["blender", "--background", "--python", test, "--", *sys.argv[2:]]
    runpy.run_path(test, run_name="__main__")


if __name__ == "__main__":
    main()
