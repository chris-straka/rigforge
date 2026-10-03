# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless smoke test: enable rigforge, add the human metarig, generate.

Run like the suites (tools/run_test.sh or tools/run_test_bpy.py).
Pass criteria: exits 0 and prints RIGFORGE_SMOKE_OK.
"""

import sys

import bpy

bpy.ops.preferences.addon_enable(module="rigforge")

import rigforge  # noqa: E402

if "rf_scripts" not in rigforge.__file__:
    print(f"RIGFORGE_SMOKE_FAIL: loaded wrong copy: {rigforge.__file__}")
    sys.exit(1)
bpy.ops.object.rigforge_human_metarig_add()
metarig = bpy.context.active_object
bpy.ops.pose.rigforge_generate()
rigs = [o for o in bpy.data.objects if o.type == "ARMATURE" and o is not metarig]
if len(rigs) != 1 or len(rigs[0].data.bones) < 700:
    print(f"RIGFORGE_SMOKE_FAIL: generated {[len(r.data.bones) for r in rigs]}")
    sys.exit(1)
print(f"RIGFORGE_SMOKE_OK ({len(rigs[0].data.bones)} bones)")
