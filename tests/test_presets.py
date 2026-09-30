# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: HLL metarig presets add, fit their subjects, and generate.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \
      /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tests/test_presets.py

Pass criteria: exits 0 and prints RIGFORGE_PRESETS_OK.
"""

import json
import os
import struct
import sys

import bpy


def fail(msg):
    print("RIGFORGE_PRESETS_FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def max_z(obj):
    return max(max(b.head_local.z, b.tail_local.z) for b in obj.data.bones)


def glb_joints(path):
    with open(path, "rb") as f:
        raw = f.read()
    json_len = struct.unpack("<I", raw[12:16])[0]
    gltf = json.loads(raw[20 : 20 + json_len])
    joints = []
    for skin in gltf.get("skins", []):
        joints += [gltf["nodes"][j].get("name") for j in skin["joints"]]
    return joints


def clean_scene():
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
        for x in list(coll):
            coll.remove(x)


def check_hero(obj):
    bones = obj.data.bones
    check(len(bones) == 159, f"hero: {len(bones)} bones, expected 159")
    for name in ("face", "thigh.L", "upper_arm.L", "f_middle.01.L", "toe.L"):
        check(name in bones, f"hero: missing bone {name}")
    height = max_z(obj)
    print(f"hero: height={height:.3f}")
    check(1.10 <= height <= 1.18, f"hero height {height:.3f}, expected ~1.14")
    hand = bones["hand.L"]
    check(
        hand.tail_local.z < 0.65 and abs(hand.tail_local.x) < 0.30,
        f"hero hand not in A-pose: tail={list(hand.tail_local)}",
    )
    check(bones["toe.L"].tail_local.y < 0, "hero should face -Y like Andras")


def check_stalker(obj):
    bones = obj.data.bones
    check(len(bones) == 70, f"stalker: {len(bones)} bones, expected 70")
    for name in ("skull", "neck.001", "tail.001", "upper_arm.L", "thigh.L"):
        check(name in bones, f"stalker: missing bone {name}")
    skull = bones["skull"]
    check(
        skull.head_local.y > 0.5,
        f"stalker should face +Y: skull head y={skull.head_local.y:.3f}",
    )
    front = bones["upper_arm.L"]
    check(
        0.2 <= front.head_local.x <= 0.4,
        f"stalker front leg spread x={front.head_local.x:.3f}, expected ~0.3",
    )
    check(
        0.4 <= front.head_local.y <= 0.7,
        f"stalker front leg y={front.head_local.y:.3f}, expected ~0.55",
    )
    hind = bones["thigh.L"]
    check(
        -0.7 <= hind.head_local.y <= -0.4,
        f"stalker hind leg y={hind.head_local.y:.3f}, expected ~-0.55",
    )
    spine_z = bones["spine.001"].head_local.z
    check(0.9 <= spine_z <= 1.15, f"stalker spine z={spine_z:.3f}, expected ~1.04")


PRESETS = (
    ("rigforge_hll_hero_metarig_add", "hero", check_hero),
    ("rigforge_hll_stalker_metarig_add", "stalker", check_stalker),
)

bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__

for op_name, label, check_fn in PRESETS:
    clean_scene()
    getattr(bpy.ops.object, op_name)()
    meta = bpy.context.view_layer.objects.active
    print(f"{label}: metarig {meta.name} bones={len(meta.data.bones)}")
    check_fn(meta)

    # Preset must generate and export a deform-only skeleton.
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.pose.rigforge_generate()
    rig = next(
        (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data),
        None,
    )
    check(rig is not None, f"{label}: generate produced no rig")
    n_def = sum(1 for b in rig.data.bones if b.name.startswith("DEF-"))
    print(f"{label}: rig bones={len(rig.data.bones)} def={n_def}")
    check(n_def > 30, f"{label}: only {n_def} DEF bones")

    bpy.ops.object.select_all(action="DESELECT")
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    path = f"/tmp/rigforge_preset_{label}.glb"
    if os.path.exists(path):
        os.remove(path)
    bpy.ops.wm.rigforge_game_export(filepath=path)
    joints = glb_joints(path)
    bad = [j for j in joints if not (j or "").startswith("DEF-")]
    check(joints, f"{label}: exported GLB has no joints")
    check(not bad, f"{label}: non-DEF joints exported: {bad[:5]}")
    print(f"{label}: export OK ({len(joints)} joints)")

print("RIGFORGE_PRESETS_OK")
