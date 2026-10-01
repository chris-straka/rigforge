# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: retarget source intake normalizes FBX import state.

Run (from the repo root; no overlay needed, no external assets):
    /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tests/test_retarget.py

Regression test for the live-clip failure: Mixamo 'for Unity' FBX clips
arrive centimeters + Y-up with the importer compensating via object
rotation+scale, which made facing detection fail
('cannot determine Y facing'). The test builds the procedural source,
puts it into exactly that import state (cm data in the file frame +
compensating object transform), proves facing fails red, normalizes,
and proves the pipeline inputs recover (facing sign, meter-scale leg
length, location keys). No Adobe assets are committed for this.

Pass criteria: exits 0 and prints RIGFORGE_RETARGET_TEST_OK.
"""

import importlib.util
import os
import sys

import bpy
from mathutils import Matrix, Vector


def check(cond, msg):
    if not cond:
        raise SystemExit(f"TEST FAIL: {msg}")
    print(f"  ok: {msg}")


def main():
    tool = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "tools", "retarget_mixamo.py"
    )
    spec = importlib.util.spec_from_file_location("rt", tool)
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    src, action = rt.build_procedural_source(frames=6)
    names = rt.src_bone_names(src)
    # Sanity on the meter/Z-up procedural source (pre-existing behavior).
    check(
        rt.facing_sign(src, names["LeftFoot"], names["LeftToeBase"]) in (1.0, -1.0),
        "procedural facing works",
    )
    leg_m = rt.leg_length_from_bones(src, names["LeftUpLeg"], names["LeftFoot"])
    check(0.5 < leg_m < 1.2, f"procedural leg {leg_m:.3f} m is sane")
    loc0 = None
    for fc in rt.all_fcurves(action):
        if fc.data_path.endswith("location") and fc.keyframe_points:
            loc0 = (fc.data_path, fc.array_index, fc.keyframe_points[0].co.y)
            break
    check(loc0 is not None, "procedural source has location keys")

    # Mimic FBX import state: cm data in the Y-up file frame + object
    # rotation+scale compensating (exactly what io_scene_fbx produces).
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode="EDIT")
    for eb in src.data.edit_bones:
        eb.use_connect = False
    rot = Matrix.Rotation(-1.5707963, 4, "X")
    for eb in src.data.edit_bones:
        eb.head = rot @ (Vector(eb.head) * 100.0)
        eb.tail = rot @ (Vector(eb.tail) * 100.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    src.rotation_euler = (1.5707963, 0.0, 0.0)
    src.scale = (0.01, 0.01, 0.01)
    for fc in rt.all_fcurves(action):
        if fc.data_path.endswith("location"):
            for kp in fc.keyframe_points:
                kp.co.y *= 100.0

    # RED: facing must fail on the un-normalized import state.
    try:
        rt.facing_sign(src, names["LeftFoot"], names["LeftToeBase"])
    except SystemExit:
        print("  ok: facing fails red on raw import state")
    else:
        raise SystemExit("TEST FAIL: facing passed on raw import state (repro broken)")

    # GREEN: normalize recovers meter/Z-up pipeline inputs.
    rt.normalize_clip_transform(src, action)
    check(
        all(abs(s - 1.0) < 1e-6 for s in src.scale),
        f"scale recovered to 1 (got {tuple(round(s, 6) for s in src.scale)})",
    )
    check(Vector(src.rotation_euler).length < 1e-6, "rotation recovered to identity")
    check(
        rt.facing_sign(src, names["LeftFoot"], names["LeftToeBase"]) in (1.0, -1.0),
        "facing works after normalize",
    )
    leg1 = rt.leg_length_from_bones(src, names["LeftUpLeg"], names["LeftFoot"])
    check(abs(leg1 - leg_m) < 1e-4, f"leg length recovered ({leg1:.4f} m)")
    for fc in rt.all_fcurves(action):
        if (fc.data_path, fc.array_index) == (loc0[0], loc0[1]):
            check(
                abs(fc.keyframe_points[0].co.y - loc0[2]) < 1e-6,
                "location key recovered to meters",
            )
            break
    print("RIGFORGE_RETARGET_TEST_OK")


if __name__ == "__main__":
    main()
    sys.exit(0)
