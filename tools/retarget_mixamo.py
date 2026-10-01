# SPDX-License-Identifier: GPL-2.0-or-later
"""Retarget Mixamo animations onto the HLL hero deform rig (v1, headless).

Pipeline: Mixamo skeleton (FBX clip, or a procedurally-built walk cycle)
-> per-frame rotation transfer with rest-pose compensation onto the
generated HLL hero rig's DEF bones -> root-motion transfer with stride
scaling -> foot-slide measure + cleanup pass -> existing game_export
baked-anim path -> GLB (Godot-ready).

Headless, from the repo root (addon overlay needed for generate+export):

    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/retarget_mixamo.py -- --prove

Live validation with a real Mixamo clip (needs the user's Adobe account):

    1. Download any animation FBX from mixamo.com (any character, the
       clip's own skeleton is used; "Skin: Without Skin" is fine).
    2. Run the same command with `--source ~/Downloads/<clip>.fbx
       --out /tmp/<clip>.glb` instead of `--prove`.
    3. Check the report: mapped bones, stride scale, foot-slide
       before/after, GLB joint + animation inventory.

Math (no eyeballing): for each mapped pair, the source bone's local
motion d (its matrix_basis channels) is conjugated into armature space
(A = R_src @ d @ R_src^-1, R = rest orientation), yaw-corrected for the
Mixamo(+Y)/HLL(-Y) facing difference (A' = Q @ A @ Q^-1), then
re-expressed in the target bone's posed-parent local frame
(basis = R_tgt^-1 @ P^-1 @ A' @ P @ R_tgt). Bone heads stay glued to
their posed parents, so chains never disconnect. Hips translation is
transferred scaled by the target/source leg-length ratio (stride scale).

Deliberate v1 limits, all documented here rather than hidden:
- Chain-root translation approximation: the DEF hierarchy does not hang
  the limbs under the pelvis (DEF-thigh's parent is the unposed
  ORG-spine), so every driven chain root is carried by the same rigid
  root offset. Ancestor-rotation-induced translation (e.g. torso twist
  moving the shoulder heads) is ignored — mm-scale for locomotion, wrong
  for acrobatics (a backflip would rotate the legs in place). A
  control-rig retarget is the v2 fix for that class.
- DEF-spine.005 (mid-neck) has no Mixamo counterpart; it rigid-follows
  DEF-spine.004 instead of freezing at rest (same for any undriven DEF
  bone under a driven one, e.g. distal finger joints).
- Fingers map proximal phalanges only (Mixamo *1 -> DEF *.01); distal
  joints rarely animate in Mixamo clips. Extend by copying the pattern.
- Foot pinning is root-drift compensation, not IK: stance-phase drift
  of each foot is ramped out through the root location curves. Residual
  slide is measured and reported honestly (see --no-pin to compare).
- The driven DEF bones get their rig constraints muted: the session is
  export-dedicated (generate -> retarget -> export), not for round-trip
  control-rig editing.
"""

import argparse
import json
import math
import os
import struct
import sys

import bpy
from mathutils import Matrix, Vector

# ---------------------------------------------------------------------------
# Correspondence map: Mixamo short bone name -> HLL hero DEF bone.
# Keys omit the "mixamorig:" FBX prefix (see norm_src_name); extend by
# adding rows. 32 entries: 6 spine + 16 limbs + 10 finger proximals.
# ---------------------------------------------------------------------------
MIXAMO_TO_DEF = {
    # Spine chain (Mixamo 6 -> DEF 6 of 7; DEF-spine.005 stays at rest).
    "Hips": "DEF-spine",
    "Spine": "DEF-spine.001",
    "Spine1": "DEF-spine.002",
    "Spine2": "DEF-spine.003",
    "Neck": "DEF-spine.004",
    "Head": "DEF-spine.006",
    # Left arm / leg.
    "LeftShoulder": "DEF-shoulder.L",
    "LeftArm": "DEF-upper_arm.L",
    "LeftForeArm": "DEF-forearm.L",
    "LeftHand": "DEF-hand.L",
    "LeftUpLeg": "DEF-thigh.L",
    "LeftLeg": "DEF-shin.L",
    "LeftFoot": "DEF-foot.L",
    "LeftToeBase": "DEF-toe.L",
    # Right arm / leg.
    "RightShoulder": "DEF-shoulder.R",
    "RightArm": "DEF-upper_arm.R",
    "RightForeArm": "DEF-forearm.R",
    "RightHand": "DEF-hand.R",
    "RightUpLeg": "DEF-thigh.R",
    "RightLeg": "DEF-shin.R",
    "RightFoot": "DEF-foot.R",
    "RightToeBase": "DEF-toe.R",
    # Finger proximals (extend with *2/*3 -> DEF *.02/*.03 as needed).
    "LeftHandThumb1": "DEF-thumb.01.L",
    "LeftHandIndex1": "DEF-f_index.01.L",
    "LeftHandMiddle1": "DEF-f_middle.01.L",
    "LeftHandRing1": "DEF-f_ring.01.L",
    "LeftHandPinky1": "DEF-f_pinky.01.L",
    "RightHandThumb1": "DEF-thumb.01.R",
    "RightHandIndex1": "DEF-f_index.01.R",
    "RightHandMiddle1": "DEF-f_middle.01.R",
    "RightHandRing1": "DEF-f_ring.01.R",
    "RightHandPinky1": "DEF-f_pinky.01.R",
}

# Extra DEF bones driven with the SAME source delta as their drive bone:
# limb twist segments (.001 sit between the mapped bones in the DEF
# hierarchy, so driving them keeps the basis math parent-consistent),
# plus pelvis/breast plates following their torso segment.
FOLLOWERS = {
    "DEF-upper_arm.L.001": "LeftArm",
    "DEF-upper_arm.R.001": "RightArm",
    "DEF-forearm.L.001": "LeftForeArm",
    "DEF-forearm.R.001": "RightForeArm",
    "DEF-thigh.L.001": "LeftUpLeg",
    "DEF-thigh.R.001": "RightUpLeg",
    "DEF-shin.L.001": "LeftLeg",
    "DEF-shin.R.001": "RightLeg",
    "DEF-pelvis.L": "Hips",
    "DEF-pelvis.R": "Hips",
    "DEF-breast.L": "Spine2",
    "DEF-breast.R": "Spine2",
}

ROOT_SRC = "Hips"  # the one mapped bone whose translation transfers
FEET_SRC = (("LeftFoot", "LeftToeBase"), ("RightFoot", "RightToeBase"))
FEET_TGT = ("DEF-foot.L", "DEF-foot.R")

PROVE_FRAMES = 48
PROVE_FPS = 30
PROVE_OUT = "/tmp/rigforge_retarget.glb"


def fail(msg):
    print(f"RIGFORGE_RETARGET_FAIL: {msg}")
    sys.exit(1)


def all_fcurves(action):
    """Every fcurve in an action, layered (Blender 5.x) or legacy."""
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                out.extend(bag.fcurves)
    return out


def find_fcurve(action, data_path, index):
    for fc in all_fcurves(action):
        if fc.data_path == data_path and fc.array_index == index:
            return fc
    return None


def norm_src_name(name):
    """Strip Mixamo FBX name decoration to the MIXAMO_TO_DEF key form."""
    short = name.split(":")[-1]
    if short.lower().startswith("mixamorig_"):
        short = short[len("mixamorig_") :]
    return short


# ---------------------------------------------------------------------------
# Procedural proof source: mixamorig skeleton + scripted walk cycle.
# Faces +Y like a real Mixamo FBX import; 1.80 m tall so stride scaling
# (hero is 1.14 m) is exercised, not assumed.
# ---------------------------------------------------------------------------
def _src_skeleton_spec():
    """(name, head, tail, parent) in Blender units, facing +Y."""
    spine = [
        ("Hips", (0, 0, 0.98), (0, 0, 1.10), None),
        ("Spine", (0, 0, 1.10), (0, 0, 1.26), "Hips"),
        ("Spine1", (0, 0, 1.26), (0, 0, 1.42), "Spine"),
        ("Spine2", (0, 0, 1.42), (0, 0, 1.55), "Spine1"),
        ("Neck", (0, 0, 1.55), (0, 0, 1.63), "Spine2"),
        ("Head", (0, 0, 1.63), (0, 0, 1.80), "Neck"),
    ]
    limbs = []
    for side, s in (("Left", 1.0), ("Right", -1.0)):
        limbs += [
            (f"{side}Shoulder", (0.06 * s, 0, 1.52), (0.20 * s, 0, 1.52), "Spine2"),
            (f"{side}Arm", (0.20 * s, 0, 1.52), (0.48 * s, 0, 1.52), f"{side}Shoulder"),
            (
                f"{side}ForeArm",
                (0.48 * s, 0, 1.52),
                (0.74 * s, 0, 1.52),
                f"{side}Arm",
            ),
            (f"{side}Hand", (0.74 * s, 0, 1.52), (0.90 * s, 0, 1.52), f"{side}ForeArm"),
            (f"{side}UpLeg", (0.11 * s, 0, 0.98), (0.11 * s, 0, 0.52), "Hips"),
            (f"{side}Leg", (0.11 * s, 0, 0.52), (0.11 * s, 0, 0.10), f"{side}UpLeg"),
            (
                f"{side}Foot",
                (0.11 * s, 0, 0.10),
                (0.11 * s, 0.14, 0.03),
                f"{side}Leg",
            ),
            (
                f"{side}ToeBase",
                (0.11 * s, 0.14, 0.03),
                (0.11 * s, 0.24, 0.02),
                f"{side}Foot",
            ),
        ]
    return spine + limbs


def build_procedural_source(frames=PROVE_FRAMES, fps=PROVE_FPS):
    """Build the mixamorig source armature and key a walk cycle on it."""
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.armature_add(enter_editmode=False, location=(0, 0, 0))
    src = bpy.context.view_layer.objects.active
    src.name = "MixamoSource"
    arm = src.data
    arm.name = "MixamoSource"
    bpy.ops.object.mode_set(mode="EDIT")
    for b in list(arm.edit_bones):
        arm.edit_bones.remove(b)
    made = {}
    for name, head, tail, parent in _src_skeleton_spec():
        b = arm.edit_bones.new(f"mixamorig:{name}")
        b.head, b.tail = head, tail
        b.roll = 0.0
        if parent is not None:
            b.parent = made[parent]
            if (Vector(b.head) - Vector(made[parent].tail)).length < 1e-4:
                b.use_connect = True
        made[name] = b
    bpy.ops.object.mode_set(mode="POSE")
    for pb in src.pose.bones:
        pb.rotation_mode = "XYZ"

    scene = bpy.context.scene
    scene.render.fps = fps
    scene.frame_start, scene.frame_end = 1, frames
    action = bpy.data.actions.new("ProceduralWalk")
    src.animation_data_create()
    src.animation_data.action = action

    rest_rot = {pb.name: pb.bone.matrix.to_3x3() for pb in src.pose.bones}

    def swing(full_name, axis, angle, frame):
        # Express a world-axis rotation in the bone's local frame so the
        # scripted motion reads the same regardless of rest orientation.
        pb = src.pose.bones[full_name]
        r = rest_rot[full_name]
        local = r.inverted() @ Matrix.Rotation(angle, 3, axis) @ r
        scene.frame_set(frame)
        pb.matrix_basis = local.to_4x4()
        pb.keyframe_insert("rotation_euler", frame=frame)

    hips_r = rest_rot["mixamorig:Hips"]
    # Amplitudes are matched to the root travel so stance feet genuinely
    # plant: leg swing L*A*w ~= root speed (48 f @ 30 fps, 1.2 m travel).
    travel = 1.2  # metres of +Y root travel over the clip
    for f in range(1, frames + 1):
        i = f - 1
        ph = 2.0 * math.pi * i / frames
        scene.frame_set(f)
        # Root: travel + sway + double-frequency bob.
        off = Vector(
            (
                0.02 * math.sin(ph),
                travel * i / (frames - 1),
                0.025 * math.cos(2.0 * ph),
            )
        )
        hips = src.pose.bones["mixamorig:Hips"]
        hips.location = hips_r.inverted() @ off
        hips.keyframe_insert("location", frame=f)
        swing("mixamorig:Hips", "Z", 0.06 * math.sin(ph), f)
        # Legs: opposite-phase swing, knee flexes through the swing.
        for side, s in (("Left", 0.0), ("Right", math.pi)):
            p = ph + s
            up = 0.22 * math.sin(p)
            knee = -0.14 * (1.0 + math.cos(p - 0.4))
            swing(f"mixamorig:{side}UpLeg", "X", up, f)
            swing(f"mixamorig:{side}Leg", "X", knee, f)
            swing(f"mixamorig:{side}Foot", "X", -(up + knee) * 0.75, f)
            swing(
                f"mixamorig:{side}ToeBase", "X", 0.30 * max(0.0, math.sin(p + 2.4)), f
            )
            # Arms oppose the same-side leg; elbows hold a soft bend.
            arm_sw = -0.15 * math.sin(p)
            bend = 0.20 if side == "Left" else -0.20
            swing(f"mixamorig:{side}Shoulder", "Z", 0.03 * math.sin(p), f)
            swing(f"mixamorig:{side}Arm", "Z", arm_sw, f)
            swing(f"mixamorig:{side}ForeArm", "Z", arm_sw + bend, f)
            swing(f"mixamorig:{side}Hand", "Z", arm_sw + bend, f)
        # Torso: slight lean + yaw against the pelvis.
        swing("mixamorig:Spine", "X", 0.04 + 0.02 * math.sin(2.0 * ph), f)
        swing("mixamorig:Spine1", "Z", 0.08 * math.sin(ph), f)
        swing("mixamorig:Spine2", "Z", 0.05 * math.sin(ph), f)
        swing("mixamorig:Neck", "X", -0.02 * math.sin(2.0 * ph), f)

    scene.frame_set(1)
    bpy.context.view_layer.update()
    print(f"procedural source: {len(made)} bones, {frames} frames @ {fps} fps")
    return src, action


# ---------------------------------------------------------------------------
# Real-clip input + shared characterization helpers.
# ---------------------------------------------------------------------------
def find_mixamo_armature():
    """Armature whose bones best match the correspondence map, else None."""
    best, best_hits = None, 0
    for obj in bpy.data.objects:
        if obj.type != "ARMATURE" or "rig_id" in obj.data:
            continue
        shorts = {norm_src_name(b.name) for b in obj.data.bones}
        hits = len(shorts & set(MIXAMO_TO_DEF))
        if hits > best_hits:
            best, best_hits = obj, hits
    return best if best_hits >= 8 else None


def find_clip_action(arm):
    """Action driving the source armature (active one, else best scan)."""
    if arm.animation_data and arm.animation_data.action:
        return arm.animation_data.action
    names = {b.name for b in arm.data.bones}
    names |= {norm_src_name(n) for n in names}
    best, best_hits = None, 0
    for action in bpy.data.actions:
        paths = " ".join(fc.data_path for fc in all_fcurves(action))
        hits = sum(1 for n in names if f'"{n}"' in paths)
        if hits > best_hits:
            best, best_hits = action, hits
    return best if best_hits >= 4 else None


def import_mixamo_clip(path):
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
    except Exception as exc:
        print(f"fbx addon enable note: {exc}")
    if not hasattr(bpy.ops.import_scene, "fbx"):
        fail("FBX importer unavailable (io_scene_fbx not enabled)")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path)
    fresh = [o for o in bpy.data.objects if o not in before]
    print(f"fbx import: {len(fresh)} new objects from {path}")
    arm = find_mixamo_armature()
    if arm is None:
        fail(f"no mixamorig skeleton found in {path}")
    action = find_clip_action(arm)
    normalize_clip_transform(arm, action)
    if action is None:
        fail(f"no animation action found driving {arm.name} in {path}")
    lo, hi = action.frame_range
    frames = list(range(math.floor(lo), math.ceil(hi) + 1))
    if len(frames) < 2:
        fail(f"clip action {action.name} covers < 2 frames")
    print(
        f"clip: armature={arm.name} action={action.name} frames={frames[0]}..{frames[-1]}"
    )
    return arm, action, frames


def normalize_clip_transform(arm, action):
    """Bake the FBX import's object transform into meters/Z-up data.

    Mixamo 'for Unity' clips arrive centimeters + Y-up: the importer keeps
    raw data and compensates with object rotation+scale (0.01, +90deg X).
    Downstream code (facing, rest orientations, stride) assumes meter-scale
    Z-up data, so apply rotation+scale and scale location fcurves by the
    uniform factor. Rotation channels are parent-relative and untouched.
    """
    s = tuple(arm.scale)
    if abs(s[0] - s[1]) > 1e-6 or abs(s[0] - s[2]) > 1e-6:
        fail(f"non-uniform clip scale {s} — unsupported")
    if abs(s[0] - 1.0) < 1e-6 and Vector(arm.rotation_euler).length < 1e-6:
        return
    prev_active = bpy.context.view_layer.objects.active
    prev_sel = [o for o in bpy.data.objects if o.select_get()]
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.ops.object.select_all(action="DESELECT")
    for o in prev_sel:
        o.select_set(True)
    bpy.context.view_layer.objects.active = prev_active
    if abs(s[0] - 1.0) > 1e-6:
        for fc in all_fcurves(action):
            if fc.data_path.endswith("location"):
                for kp in fc.keyframe_points:
                    kp.co.y *= s[0]
                    kp.handle_left.y *= s[0]
                    kp.handle_right.y *= s[0]
    print(f"clip normalize: baked scale {s[0]:.4f} + object rotation")


def facing_sign(arm, foot_name, toe_name):
    """+1 if the rig faces +Y, -1 if -Y, from the foot->toe direction."""
    foot = arm.data.bones.get(foot_name)
    toe = arm.data.bones.get(toe_name)
    if foot is None or toe is None:
        fail(f"facing probe bones missing: {foot_name}, {toe_name}")
    v = toe.tail_local - foot.head_local
    if abs(v.y) < abs(v.x) or abs(v.y) < abs(v.z):
        fail(f"cannot determine Y facing from foot vector {tuple(v)}")
    return 1.0 if v.y > 0 else -1.0


def src_bone_names(src):
    """Map MIXAMO_TO_DEF short names -> actual source bone names."""
    out = {}
    for b in src.data.bones:
        short = norm_src_name(b.name)
        if short in MIXAMO_TO_DEF and short not in out:
            out[short] = b.name
    return out


def leg_length_from_bones(arm, upper_name, foot_name):
    upper = arm.data.bones.get(upper_name)
    foot = arm.data.bones.get(foot_name)
    if upper is None or foot is None:
        fail(f"leg probe bones missing: {upper_name}, {foot_name}")
    return (upper.head_local - foot.head_local).length


def rest_parent_rel(arm, bone_name):
    """Parent-relative rest 4x4 R with pose-matrix relation M = P @ R @ B."""
    bone = arm.data.bones[bone_name]
    if bone.parent is None:
        return bone.matrix_local.copy()
    return (
        Matrix.Translation(bone.parent.matrix_local.inverted() @ bone.head_local)
        @ bone.matrix.to_4x4()
    )


def bone_depth(arm, bone_name):
    n, b = 0, arm.data.bones[bone_name]
    while b.parent is not None:
        n += 1
        b = b.parent
    return n


# ---------------------------------------------------------------------------
# Retarget core.
# ---------------------------------------------------------------------------
def build_drive_plan(src, tgt):
    """Runtime drive plan: which DEF bones move, and how each is carried.

    Returns (drive, chain_roots, rigid, skipped): drive maps DEF bone ->
    source short name; chain_roots are driven bones with no driven ancestor
    (each gets the root-motion offset, since the DEF hierarchy — unlike
    Mixamo's — does not hang the limbs under the pelvis: DEF-thigh hangs
    off the unposed ORG-spine, so without this the pelvis would travel
    while the legs march in place); rigid are undriven DEF bones under
    driven ones (e.g. DEF-spine.005), which rigidly follow with muted
    constraints instead of freezing at rest.
    """
    shorts = src_bone_names(src)
    drive = {}  # tgt DEF bone -> source short name
    skipped = []
    for short, tgt_name in MIXAMO_TO_DEF.items():
        if short not in shorts:
            skipped.append(f"{short} (no source bone)")
        elif tgt_name not in tgt.data.bones:
            skipped.append(f"{short} (no {tgt_name})")
        else:
            drive[tgt_name] = short
    for tgt_name, short in FOLLOWERS.items():
        if short in shorts and tgt_name in tgt.data.bones and tgt_name not in drive:
            drive[tgt_name] = short
    if not drive:
        fail("correspondence map matched zero bones")
    root_tgt = MIXAMO_TO_DEF[ROOT_SRC]
    if root_tgt not in drive:
        fail("root pair Hips->DEF-spine did not match; cannot transfer root motion")

    def has_driven_ancestor(tgt_name):
        b = tgt.data.bones[tgt_name].parent
        while b is not None:
            if b.name in drive:
                return True
            b = b.parent
        return False

    chain_roots = sorted(
        [t for t in drive if not has_driven_ancestor(t)],
        key=lambda n: bone_depth(tgt, n),
    )
    rigid = sorted(
        b.name
        for b in tgt.data.bones
        if b.name.startswith("DEF-")
        and b.name not in drive
        and has_driven_ancestor(b.name)
    )
    return drive, chain_roots, rigid, skipped


def retarget(src, tgt, frames, stride, yaw_flip, plan, action_name="Retargeted"):
    """Transfer the source action onto the target rig's DEF bones.

    Returns (action, stats). Both armatures must sit at identity object
    transforms (checked); the driven DEF bones get rotation_mode
    QUATERNION and their rig constraints muted (export-dedicated session).
    """
    scene = bpy.context.scene
    if (
        max(abs(v) for row in list(src.matrix_world - Matrix()) for v in row) > 1e-6
        or max(abs(v) for row in list(tgt.matrix_world - Matrix()) for v in row) > 1e-6
    ):
        fail("source and target armatures must be at identity transforms")
    shorts = src_bone_names(src)
    drive, chain_roots, rigid, skipped = plan
    roots = set(chain_roots)
    order = sorted(drive, key=lambda n: bone_depth(tgt, n))

    for tgt_name in drive:
        pb = tgt.pose.bones[tgt_name]
        pb.rotation_mode = "QUATERNION"
        for c in pb.constraints:
            c.mute = True
    for tgt_name in rigid:
        for c in tgt.pose.bones[tgt_name].constraints:
            c.mute = True
    for tgt_name in chain_roots:
        tgt.pose.bones[tgt_name].lock_location = (False, False, False)

    # Rest snapshots (armature space = world, transforms are identity).
    r_src = {
        s: src.data.bones[shorts[s]].matrix_local.to_3x3() for s in set(drive.values())
    }
    r_src_head = src.data.bones[shorts[ROOT_SRC]].matrix_local.translation.copy()
    r_tgt = {t: tgt.data.bones[t].matrix_local.copy() for t in drive}
    r_tgt_rel = {t: rest_parent_rel(tgt, t) for t in drive}
    parent_rest = {}
    for t in drive:
        parent = tgt.data.bones[t].parent
        parent_rest[t] = parent.matrix_local.copy() if parent else Matrix()
    q_fix = Matrix.Rotation(math.pi, 3, "Z") if yaw_flip else Matrix.Identity(3)

    action = bpy.data.actions.new(action_name)
    tgt.animation_data_create()
    tgt.animation_data.action = action

    src_hips = src.pose.bones[shorts[ROOT_SRC]]
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        # Strided root offset, shared by every driven chain root (see plan).
        off = q_fix @ (src_hips.matrix.translation - r_src_head) * stride
        desired = {}  # tgt bone -> desired armature-space 4x4 this frame
        for tgt_name in order:
            short = drive[tgt_name]
            src_pb = src.pose.bones[shorts[short]]
            # Source local motion -> armature-space rotation change -> yaw fix.
            d = src_pb.matrix_basis.to_3x3()
            a_src = r_src[short] @ d @ r_src[short].inverted()
            a_tgt = q_fix @ a_src @ q_fix.inverted()
            # Re-express in the target bone's posed-parent local frame.
            parent = tgt.data.bones[tgt_name].parent
            p = (
                desired[parent.name]
                if parent and parent.name in desired
                else parent_rest[tgt_name]
            )
            p3, r3 = p.to_3x3(), r_tgt_rel[tgt_name].to_3x3()
            basis3 = r3.inverted() @ p3.inverted() @ a_tgt @ p3 @ r3
            q = basis3.to_quaternion()
            q.normalize()
            pb = tgt.pose.bones[tgt_name]
            basis4 = q.to_matrix().to_4x4()
            if tgt_name in roots:
                head = r_tgt[tgt_name].translation + off
                pr = (p @ r_tgt_rel[tgt_name]).translation
                loc = (p3 @ r3).inverted() @ (head - pr)
                pb.location = loc
                basis4.translation = loc
                pb.keyframe_insert("location", frame=f)
            pb.rotation_quaternion = (q.w, q.x, q.y, q.z)
            pb.keyframe_insert("rotation_quaternion", frame=f)
            desired[tgt_name] = p @ r_tgt_rel[tgt_name] @ basis4

    stats = {
        "mapped": len(drive),
        "core_mapped": sum(1 for t in drive if t in set(MIXAMO_TO_DEF.values())),
        "chain_roots": chain_roots,
        "rigid_follow": rigid,
        "skipped": skipped,
        "muted_constraints": sum(
            1
            for t in list(drive) + rigid
            for c in tgt.pose.bones[t].constraints
            if c.mute
        ),
    }
    print(
        f"retarget: {stats['mapped']} DEF bones driven "
        f"({len(chain_roots)} chain roots, {len(rigid)} rigid-follow), "
        f"{len(skipped)} map rows skipped"
    )
    for s in skipped:
        print(f"  skip: {s}")
    return action, stats


# ---------------------------------------------------------------------------
# Foot-slide measurement + cleanup pass.
# ---------------------------------------------------------------------------
def foot_tracks(tgt, feet, frames):
    """Evaluated world-space head positions per foot bone per frame."""
    scene = bpy.context.scene
    tracks = {name: [] for name in feet}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for name in feet:
            p = tgt.matrix_world @ tgt.pose.bones[name].matrix.translation
            tracks[name].append(p.copy())
    return tracks


def stance_mask(pts, fps):
    """Stance frames: bottom 35% of foot height AND below-median speed.

    The speed gate excludes mid-swing frames, where the foot passes low
    but fast — without it, "stance" intervals span swing phases, their net
    drift cancels, and the pin pass corrects nothing (observed). Returns
    (mask, per-frame-start speeds); the last frame reuses the final speed.
    """
    speeds = [
        Vector((pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y, 0)).length * fps
        for i in range(len(pts) - 1)
    ]
    med = sorted(speeds)[len(speeds) // 2] if speeds else 0.0
    per_frame = speeds + ([speeds[-1]] if speeds else [0.0])
    zs = [p.z for p in pts]
    lo, hi = min(zs), max(zs)
    mask = [
        (z <= lo + 0.35 * (hi - lo)) and (s <= med)
        for z, s in zip(zs, per_frame, strict=True)
    ]
    return mask, speeds


def measure_foot_slide(tgt, feet, frames, fps):
    """Per-foot stance-phase horizontal speed stats (m/s, honestly measured).

    Stance = foot head in the bottom 35% of its height range AND moving
    slower than its own median (see stance_mask). Speeds are frame-to-frame
    horizontal displacements x fps.
    """
    tracks = foot_tracks(tgt, feet, frames)
    out = {}
    for name, pts in tracks.items():
        stance, speeds = stance_mask(pts, fps)
        st = [s for i, s in enumerate(speeds) if stance[i]]
        out[name] = {
            "stance_frames": sum(stance),
            "stance_mean_m_s": round(sum(st) / len(st), 4) if st else 0.0,
            "stance_max_m_s": round(max(st), 4) if st else 0.0,
            "overall_max_m_s": round(max(speeds), 4) if speeds else 0.0,
        }
    return out


def pin_feet(tgt, feet, frames, chain_roots):
    """Crude-but-working cleanup: ramp stance drift out through the roots.

    For each stance interval of each foot, the foot's horizontal drift is
    subtracted as a rigid world shift on a linear ramp. The shift goes to
    every driven chain root (DEF-spine plus the limb/arm chain roots —
    correcting DEF-spine alone would not move the ORG-parented legs).
    Both feet accumulate (double support averages out); Z is untouched.
    This is not IK — residual slide remains and is re-measured by the
    caller.
    """
    action = tgt.animation_data.action
    if action is None:
        fail("pin_feet needs the retargeted action on the target rig")
    # Per-target world->location-channel converters. Location channels
    # live in each bone's local frame (for DEF-spine, local Y is ~world
    # Z), so world drift must be converted, not copied per-axis. Chain
    # roots' parents are never keyed, so rest matrices are exact here.
    targets = {}
    for name in chain_roots:
        rel = rest_parent_rel(tgt, name).to_3x3()
        parent = tgt.data.bones[name].parent
        par3 = parent.matrix_local.to_3x3().copy() if parent else Matrix.Identity(3)
        fcurves = {
            i: find_fcurve(action, f'pose.bones["{name}"].location', i)
            for i in (0, 1, 2)
        }
        if not all(fcurves.values()):
            fail(f"pin_feet needs keyed location curves on {name}")
        targets[name] = (
            (par3 @ rel).inverted(),
            fcurves,
            {
                i: {int(kp.co.x): kp for kp in fc.keyframe_points}
                for i, fc in fcurves.items()
            },
        )
    tracks = foot_tracks(tgt, feet, frames)
    fixed_intervals, applied, total_drift = 0, 0, 0.0
    for pts in tracks.values():
        stance, _speeds = stance_mask(pts, bpy.context.scene.render.fps)
        i = 0
        while i < len(frames):
            if not stance[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(frames) and stance[j + 1]:
                j += 1
            if j > i:
                drift = Vector((pts[j].x - pts[i].x, pts[j].y - pts[i].y, 0.0))
                total_drift += drift.length
                for k in range(i, j + 1):
                    world = drift * ((k - i) / (j - i))
                    for to_local, _fcurves, keys in targets.values():
                        corr = to_local @ world
                        for axis in (0, 1, 2):
                            kp = keys[axis].get(frames[k])
                            if kp is not None:
                                kp.co.y -= corr[axis]
                                kp.handle_left.y -= corr[axis]
                                kp.handle_right.y -= corr[axis]
                                applied += 1
                fixed_intervals += 1
            i = j + 1
    for _to_local, fcurves, _keys in targets.values():
        for fc in fcurves.values():
            fc.update()
    print(
        f"pin_feet: {fixed_intervals} intervals, {applied} keys "
        f"on {len(targets)} chain roots, total drift {total_drift:.4f} m"
    )
    return fixed_intervals


def motion_proof(tgt, frames):
    """Numerics proving motion transferred (swing, travel, body coherence)."""
    scene = bpy.context.scene
    quats, roots, thighs = [], [], []
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        quats.append(tgt.pose.bones["DEF-thigh.L"].matrix_basis.to_quaternion())
        roots.append(tgt.pose.bones[MIXAMO_TO_DEF[ROOT_SRC]].matrix.translation.copy())
        thighs.append(tgt.pose.bones["DEF-thigh.L"].matrix.translation.copy())
    q0 = quats[0]
    swing = max(2.0 * math.acos(min(1.0, abs(q0.dot(q)))) for q in quats)
    travel = (roots[-1] - roots[0]).length
    thigh_travel = (thighs[-1] - thighs[0]).length
    return {
        "thigh_swing_rad": round(swing, 4),
        "root_travel_m": round(travel, 4),
        "thigh_travel_m": round(thigh_travel, 4),
    }


def glb_report(path):
    with open(path, "rb") as f:
        raw = f.read()
    json_len = struct.unpack("<I", raw[12:16])[0]
    gltf = json.loads(raw[20 : 20 + json_len])
    joints = []
    for skin in gltf.get("skins", []):
        joints += [gltf["nodes"][j].get("name") for j in skin["joints"]]
    clips = []
    for clip in gltf.get("animations", []):
        targets = [
            gltf["nodes"][ch["target"]["node"]].get("name") for ch in clip["channels"]
        ]
        clips.append(
            {"name": clip.get("name"), "channels": len(targets), "targets": targets}
        )
    return {
        "bytes": len(raw),
        "meshes": [m.get("name") for m in gltf.get("meshes", [])],
        "joints": len(joints),
        "non_def_joints": [j for j in joints if not (j or "").startswith("DEF-")],
        "animations": [{"name": c["name"], "channels": c["channels"]} for c in clips],
        "non_def_channels": sorted(
            {t for c in clips for t in c["targets"] if not (t or "").startswith("DEF-")}
        ),
    }


# ---------------------------------------------------------------------------
# Session assembly: hero rig, proof meshes, export.
# ---------------------------------------------------------------------------
def clean_scene():
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
        for x in list(coll):
            coll.remove(x)


def generate_hero():
    # NOTE: no mode_set here — clean_scene leaves no active object behind.
    bpy.ops.object.rigforge_hll_hero_metarig_add()
    bpy.ops.object.mode_set(mode="OBJECT")
    meta = bpy.context.view_layer.objects.active
    bpy.ops.pose.rigforge_generate()
    rig = next(
        (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data),
        None,
    )
    if rig is None:
        fail("hero generate produced no rig")
    n_def = sum(1 for b in rig.data.bones if b.name.startswith("DEF-"))
    print(f"hero: {meta.name} -> {rig.name} ({len(rig.data.bones)} bones, {n_def} DEF)")
    return rig


def bind_proof_meshes(rig):
    """Two cubes skinned to torso + head so the GLB proves skinning too."""
    bpy.ops.object.mode_set(mode="OBJECT")
    bound = []
    for bone_name, loc in (
        ("DEF-spine.002", (0, 0, 0.79)),
        ("DEF-spine.006", (0, 0, 1.08)),
    ):
        bpy.ops.mesh.primitive_cube_add(size=0.16, location=loc)
        cube = bpy.context.view_layer.objects.active
        mod = cube.modifiers.new("Armature", "ARMATURE")
        mod.object = rig
        vg = cube.vertex_groups.new(name=bone_name)
        vg.add(range(len(cube.data.vertices)), 1.0, "REPLACE")
        bound.append(cube.name)
    print(f"proof meshes bound: {bound}")
    return bound


def export_game_glb(rig, path):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    if os.path.exists(path):
        os.remove(path)
    bpy.ops.wm.rigforge_game_export(filepath=path)
    if not os.path.exists(path):
        fail(f"game export wrote no file to {path}")


def run_pipeline(src, tgt, frames, fps, out_path, do_pin, action_name):
    """Characterize -> retarget -> measure -> pin -> export. Returns report."""
    src_names = src_bone_names(src)
    src_facing = facing_sign(src, src_names["LeftFoot"], src_names["LeftToeBase"])
    tgt_facing = facing_sign(tgt, "DEF-foot.L", "DEF-toe.L")
    yaw_flip = src_facing != tgt_facing
    src_leg = leg_length_from_bones(src, src_names["LeftUpLeg"], src_names["LeftFoot"])
    tgt_leg = leg_length_from_bones(tgt, "DEF-thigh.L", "DEF-foot.L")
    stride = tgt_leg / src_leg
    print(
        f"facing: src={' +Y' if src_facing > 0 else ' -Y'} tgt={' +Y' if tgt_facing > 0 else ' -Y'} yaw_flip={yaw_flip}"
    )
    print(f"stride: src_leg={src_leg:.4f} tgt_leg={tgt_leg:.4f} scale={stride:.4f}")

    plan = build_drive_plan(src, tgt)
    _, stats = retarget(src, tgt, frames, stride, yaw_flip, plan, action_name)
    src_feet = (src_names["LeftFoot"], src_names["RightFoot"])
    slide_src = measure_foot_slide(src, src_feet, frames, fps)
    print(f"foot-slide source clip: {json.dumps(slide_src)}")
    slide_before = measure_foot_slide(tgt, FEET_TGT, frames, fps)
    print(f"foot-slide before pin: {json.dumps(slide_before)}")
    intervals = pin_feet(tgt, FEET_TGT, frames, plan[1]) if do_pin else 0
    slide_after = measure_foot_slide(tgt, FEET_TGT, frames, fps)
    print(f"foot-slide after pin: {json.dumps(slide_after)}")
    proof = motion_proof(tgt, frames)
    print(f"motion proof: {json.dumps(proof)}")
    if proof["thigh_swing_rad"] < 0.15:
        fail(f"no leg motion transferred (swing {proof['thigh_swing_rad']} rad)")
    if proof["root_travel_m"] < 0.05:
        fail(f"no root motion transferred (travel {proof['root_travel_m']} m)")
    if abs(proof["thigh_travel_m"] - proof["root_travel_m"]) > 0.02:
        fail(
            "legs not carried with the pelvis "
            f"(thigh {proof['thigh_travel_m']} m vs root {proof['root_travel_m']} m)"
        )

    # Drop the source rig + every foreign action: the glTF exporter bakes
    # ALL actions in the file against the exported armature, so a leftover
    # source clip would ship as a duplicate rest-pose animation (observed).
    bpy.data.objects.remove(src, do_unlink=True)
    keep = tgt.animation_data.action
    for foreign in list(bpy.data.actions):
        if foreign != keep:
            bpy.data.actions.remove(foreign)
    bpy.context.view_layer.objects.active = tgt

    bind_proof_meshes(tgt)
    export_game_glb(tgt, out_path)
    rep = glb_report(out_path)
    print(f"glb: {out_path} ({rep['bytes']} bytes, {rep['joints']} joints)")
    if rep["non_def_joints"]:
        fail(f"non-DEF joints in GLB: {rep['non_def_joints'][:5]}")
    if len(rep["animations"]) != 1:
        fail(f"expected exactly 1 animation clip, got {len(rep['animations'])}")
    if rep["non_def_channels"]:
        fail(f"animation on non-DEF nodes: {rep['non_def_channels'][:5]}")
    n_ch = sum(c["channels"] for c in rep["animations"])
    if n_ch < 10:
        fail(f"only {n_ch} animation channels baked")

    return {
        "map_entries": len(MIXAMO_TO_DEF),
        "facing": {"src": src_facing, "tgt": tgt_facing, "yaw_flip": yaw_flip},
        "stride_scale": round(stride, 4),
        "retarget": stats,
        "foot_slide_source": slide_src,
        "foot_slide_before": slide_before,
        "foot_slide_after": slide_after,
        "pin_intervals": intervals,
        "motion": proof,
        "glb": {**rep, "path": out_path},
    }


def main(argv):
    parser = argparse.ArgumentParser(
        description="Retarget Mixamo clips onto the HLL hero rig"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--prove", action="store_true", help="procedural walk-cycle proof run"
    )
    mode.add_argument("--source", help="real Mixamo clip FBX to retarget")
    parser.add_argument("--out", default=PROVE_OUT, help="output GLB path")
    parser.add_argument("--frames", type=int, default=PROVE_FRAMES, help="prove frames")
    parser.add_argument("--fps", type=int, default=PROVE_FPS, help="prove fps")
    parser.add_argument("--no-pin", action="store_true", help="skip the foot-pin pass")
    parser.add_argument("--blend", default="", help="also save the session .blend here")
    args = parser.parse_args(argv)

    bpy.ops.preferences.addon_enable(module="rigforge")
    import rigforge

    if "rf_scripts" not in rigforge.__file__:
        fail(f"loaded wrong addon copy: {rigforge.__file__}")
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    except Exception as exc:
        print(f"gltf addon enable note: {exc}")

    clean_scene()
    tgt = generate_hero()
    if args.prove:
        src, _src_action = build_procedural_source(args.frames, args.fps)
        frames = list(range(1, args.frames + 1))
        fps = args.fps
        label = "ProceduralWalk"
    else:
        if not os.path.exists(args.source):
            fail(f"source clip not found: {args.source}")
        src, _src_action, frames = import_mixamo_clip(args.source)
        fps = bpy.context.scene.render.fps
        label = os.path.splitext(os.path.basename(args.source))[0]

    report = run_pipeline(
        src, tgt, frames, fps, args.out, not args.no_pin, f"Mixamo_{label}"
    )
    if args.blend:
        bpy.ops.wm.save_as_mainfile(filepath=args.blend)

    print("RIGFORGE_RETARGET_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_RETARGET_END")
    print("RIGFORGE_RETARGET_OK")


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
