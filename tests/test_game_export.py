# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: one-click game export produces a deform-bones-only GLB.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \
      /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tests/test_game_export.py

Pass criteria: exits 0 and prints RIGFORGE_EXPORT_TEST_OK.
"""

import json
import os
import struct
import subprocess
import sys

import bpy

GLB_PATH = "/tmp/rigforge_export_test.glb"
# Helper objects the glTF importer creates on import (bone display shapes).
IMPORTER_HELPER_COLLECTION = "glTF_not_exported"


def fail(msg):
    print("RIGFORGE_EXPORT_TEST_FAIL:", msg)
    sys.exit(1)


# 1. Enable our copy of the addon (same gate as the smoke test).
bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402
from rigforge.operators import game_export as gx  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
print("enable OK:", rigforge.__file__)

# 2. Human metarig -> generate.
bpy.ops.object.rigforge_human_metarig_add()
bpy.ops.object.mode_set(mode="OBJECT")
if bpy.ops.wm.rigforge_game_export.poll():
    fail("export poll should be False on a metarig")
bpy.ops.pose.rigforge_generate()
print("generate OK")

rig = next(
    (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data),
    None,
)
if rig is None:
    fail("no generated rig found")
full_bone_count = len(rig.data.bones)
def_bones = [b.name for b in rig.data.bones if b.name.startswith("DEF-")]
print(f"rig: {rig.name} bones={full_bone_count} def_bones={len(def_bones)}")
if not def_bones:
    fail("generated rig has no DEF- bones")

# 3. Test mesh bound to the rig via one DEF bone's vertex group.
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
mesh_obj = bpy.context.view_layer.objects.active
mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
mod.object = rig
vg = mesh_obj.vertex_groups.new(name=def_bones[0])
vg.add(range(len(mesh_obj.data.vertices)), 1.0, "REPLACE")
print(f"mesh: {mesh_obj.name} bound to {def_bones[0]}")

# 4. Run the one-click export operator.
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
if not bpy.ops.wm.rigforge_game_export.poll():
    fail("export poll should be True on a generated rig")
if os.path.exists(GLB_PATH):
    os.remove(GLB_PATH)
bpy.ops.wm.rigforge_game_export(filepath=GLB_PATH)

if not os.path.exists(GLB_PATH):
    fail(f"no GLB written to {GLB_PATH}")
size = os.path.getsize(GLB_PATH)
print(f"glb: {GLB_PATH} ({size} bytes)")
if size < 1024:
    fail(f"GLB suspiciously small: {size} bytes")

# Selection must be restored after export.
if not rig.select_get() or bpy.context.view_layer.objects.active != rig:
    fail("export did not restore selection/active object")

# 4b. Skeleton-only export (no bound meshes) must succeed with 0 meshes.
mesh_name = mesh_obj.name
bpy.data.objects.remove(mesh_obj, do_unlink=True)
skel_path = "/tmp/rigforge_export_skel.glb"
if os.path.exists(skel_path):
    os.remove(skel_path)
bpy.ops.wm.rigforge_game_export(filepath=skel_path)
with open(skel_path, "rb") as f:
    skel_raw = f.read()
skel_json_len = struct.unpack("<I", skel_raw[12:16])[0]
skel = json.loads(skel_raw[20 : 20 + skel_json_len])
skel_joints = sum(len(s["joints"]) for s in skel.get("skins", []))
print(f"skeleton-only: meshes={skel.get('meshes', [])} joints={skel_joints}")
if skel.get("meshes"):
    fail(f"skeleton-only export should have no meshes: {skel['meshes']}")
if skel_joints != len(def_bones):
    fail(f"skeleton-only export has {skel_joints} joints, expected {len(def_bones)}")

# 5. Independent check: parse the GLB JSON chunk directly (no importer).
with open(GLB_PATH, "rb") as f:
    raw = f.read()
json_len = struct.unpack("<I", raw[12:16])[0]
gltf = json.loads(raw[20 : 20 + json_len])

mesh_names = [m.get("name") for m in gltf.get("meshes", [])]
joint_names = []
for skin in gltf.get("skins", []):
    joint_names += [gltf["nodes"][j].get("name") for j in skin["joints"]]
print(f"glb json: meshes={mesh_names} joints={len(joint_names)}")
if mesh_names != [mesh_name]:
    fail(f"expected exactly the skinned mesh in the GLB, got {mesh_names}")
if not joint_names:
    fail("GLB skin has no joints")
non_def_joints = [n for n in joint_names if not (n or "").startswith("DEF-")]
if non_def_joints:
    fail(f"non-DEF joints in GLB: {non_def_joints[:10]}")
if len(joint_names) != len(def_bones):
    fail(f"GLB has {len(joint_names)} joints, rig has {len(def_bones)} DEF bones")

# 5b. Baked animation: key two control bones, export, verify the GLB
# carries the clip on DEF joints only.
scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = 10
for ctl in ("root", "torso"):
    if ctl not in rig.pose.bones:
        fail(f"control bone {ctl!r} missing from generated rig")
action = bpy.data.actions.new("TestAnim")
rig.animation_data_create()
rig.animation_data.action = action
scene.frame_set(1)
rig.pose.bones["torso"].location = (0.0, 0.0, 0.0)
rig.pose.bones["torso"].keyframe_insert("location")
rig.pose.bones["root"].location = (0.0, 0.0, 0.0)
rig.pose.bones["root"].keyframe_insert("location")
scene.frame_set(10)
rig.pose.bones["torso"].location = (0.0, 0.0, 0.1)
rig.pose.bones["torso"].keyframe_insert("location")
rig.pose.bones["root"].location = (0.0, 0.5, 0.0)
rig.pose.bones["root"].keyframe_insert("location")
anim_path = "/tmp/rigforge_export_anim.glb"
if os.path.exists(anim_path):
    os.remove(anim_path)
bpy.ops.wm.rigforge_game_export(filepath=anim_path)
with open(anim_path, "rb") as f:
    anim_raw = f.read()
anim_len = struct.unpack("<I", anim_raw[12:16])[0]
anim = json.loads(anim_raw[20 : 20 + anim_len])
anims = anim.get("animations", [])
print(f"anim: {len(anims)} clips")
if not anims:
    fail("animated export contains no animation clips")
n_channels = 0
for clip in anims:
    for ch in clip["channels"]:
        n_channels += 1
        target = anim["nodes"][ch["target"]["node"]].get("name") or ""
        if not target.startswith("DEF-"):
            fail(f"animation targets non-DEF node: {target!r}")
print(f"anim: {n_channels} channels, all on DEF joints")
if n_channels < 10:
    fail(f"only {n_channels} animation channels baked")

# 5c. Mobile profile: face + twist merged up, originals untouched.
if gx.classify_def_bone("DEF-jaw.L.001") != "face":
    fail("face stem misclassified")
if gx.classify_def_bone("DEF-chin") != "face":
    fail("bare face stem misclassified")
if gx.classify_def_bone("DEF-thigh.L.001") != "twist":
    fail("twist segment misclassified")
for keep in ("DEF-spine.001", "DEF-f_index.01.L", "DEF-hand.L", "DEF-upper_arm.L"):
    if gx.classify_def_bone(keep) != "keep":
        fail(f"{keep} should survive the mobile profile")
targets = gx.mobile_merge_targets(rig)
kept = [b for b in def_bones if b not in targets]
print(f"mobile: {len(targets)} dropped, {len(kept)} kept")
if not targets or not kept or len(kept) >= len(def_bones):
    fail("mobile profile did not split the skeleton")
if any(t not in kept for t in targets.values()):
    fail("mobile merge target is not a kept bone")
if any(gx.classify_def_bone(b) == "keep" for b in targets):
    fail("kept bone scheduled for merge")

bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 2.0))
jaw_mesh = bpy.context.view_layer.objects.active
jaw_mod = jaw_mesh.modifiers.new("Armature", "ARMATURE")
jaw_mod.object = rig
jaw_vg = jaw_mesh.vertex_groups.new(name="DEF-jaw")
jaw_vg.add(range(len(jaw_mesh.data.vertices)), 1.0, "REPLACE")
jaw_target = targets.get("DEF-jaw")
if jaw_target is None:
    fail("DEF-jaw has no mobile merge target")
print(f"mobile: jaw test mesh -> {jaw_target}")
# Face roots ride the head (topmost kept bone in armature space).
if not jaw_target.startswith("DEF-spine"):
    fail(f"DEF-jaw merges into {jaw_target}, not the head/neck chain")
# A kept-bound mesh alongside (4b deleted the main cube): the mobile
# GLB must carry both, merged and unmerged.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 1.0, 0.0))
kept_mesh = bpy.context.view_layer.objects.active
kept_mod = kept_mesh.modifiers.new("Armature", "ARMATURE")
kept_mod.object = rig
kept_vg = kept_mesh.vertex_groups.new(name="DEF-spine")
kept_vg.add(range(len(kept_mesh.data.vertices)), 1.0, "REPLACE")

mobile_path = "/tmp/rigforge_export_mobile.glb"
if os.path.exists(mobile_path):
    os.remove(mobile_path)
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.wm.rigforge_game_export(filepath=mobile_path, profile="MOBILE")
with open(mobile_path, "rb") as f:
    mob_raw = f.read()
mob_len = struct.unpack("<I", mob_raw[12:16])[0]
mob = json.loads(mob_raw[20 : 20 + mob_len])
mob_joints = []
for skin in mob.get("skins", []):
    mob_joints += [mob["nodes"][j].get("name") for j in skin["joints"]]
print(f"mobile glb: joints={len(mob_joints)} (kept={len(kept)})")
if set(mob_joints) != set(kept):
    fail(
        f"mobile joints != kept set "
        f"(extra={set(mob_joints) - set(kept)}, "
        f"missing={set(kept) - set(mob_joints)})"
    )
for clip in mob.get("animations", []):
    for ch in clip["channels"]:
        target = mob["nodes"][ch["target"]["node"]].get("name") or ""
        if target not in kept:
            fail(f"mobile animation targets dropped joint: {target!r}")
print("mobile glb: joints == kept set, animations on kept joints")

# Originals untouched by the mobile path (temp copies only).
if [b.name for b in rig.data.bones if b.name.startswith("DEF-")] != def_bones:
    fail("mobile export changed the rig's DEF bones")
if any(not b.use_deform for b in rig.data.bones if b.name in targets):
    fail("mobile export toggled deform flags on the original")
if [g.name for g in jaw_mesh.vertex_groups] != ["DEF-jaw"]:
    fail("mobile export rewrote the original mesh groups")
if [o for o in bpy.data.objects if o.name.startswith("rig.")]:
    fail("mobile export left temp copies behind")
print("mobile: originals untouched, no temp leftovers")

# 5d. Validation fails early; the limit toggle caps influences.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 2.0, 0.0))
bare_mesh = bpy.context.view_layer.objects.active
bare_mod = bare_mesh.modifiers.new("Armature", "ARMATURE")
bare_mod.object = rig
errs, _ = gx.validate_export(rig, [bare_mesh], "FULL")
if [e["code"] for e in errs] != ["unweighted_mesh"]:
    fail(f"bare mesh not rejected: {errs}")
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
try:
    result = bpy.ops.wm.rigforge_game_export(filepath="/tmp/rigforge_export_bare.glb")
except RuntimeError as exc:
    if "no weights" not in str(exc):
        fail(f"wrong cancel error: {exc}")
else:
    if "CANCELLED" not in result:
        fail(f"bare mesh export not cancelled: {result}")
bpy.data.objects.remove(bare_mesh, do_unlink=True)
print("validate: unweighted mesh fails early")

bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 3.0, 0.0))
part_mesh = bpy.context.view_layer.objects.active
part_mod = part_mesh.modifiers.new("Armature", "ARMATURE")
part_mod.object = rig
part_mesh.vertex_groups.new(name="DEF-spine")
for v in part_mesh.data.vertices:
    if v.index < 4:
        part_mesh.vertex_groups[0].add([v.index], 1.0, "REPLACE")
_, warns = gx.validate_export(rig, [part_mesh], "FULL")
if [w["code"] for w in warns] != ["unweighted_verts"]:
    fail(f"partial weights not warned: {warns}")
part_mesh.scale.x = -1.0
_, warns = gx.validate_export(rig, [part_mesh], "FULL")
if "negative_scale" not in [w["code"] for w in warns]:
    fail(f"negative scale not warned: {warns}")
bpy.data.objects.remove(part_mesh, do_unlink=True)
print("validate: partial weights + negative scale warn")

multi_mesh = bpy.data.objects.new("MultiMesh", jaw_mesh.data.copy())
bpy.context.scene.collection.objects.link(multi_mesh)
multi_mod = multi_mesh.modifiers.new("Armature", "ARMATURE")
multi_mod.object = rig
five = ["DEF-jaw", "DEF-spine.001", "DEF-spine.002", "DEF-hand.L", "DEF-foot.L"]
for name in five:
    multi_mesh.vertex_groups.new(name=name)
for v in multi_mesh.data.vertices:
    for i in range(len(five)):
        multi_mesh.vertex_groups[i].add([v.index], 0.2, "REPLACE")
_, warns = gx.validate_export(rig, [multi_mesh], "MOBILE")
if "many_influences" not in [w["code"] for w in warns]:
    fail(f"5 influences not warned: {warns}")
op_cls = bpy.types.WM_OT_rigforge_game_export
rig_capped, capped, _, capped_n = op_cls._mobile_copies(
    op_cls, bpy.context, rig, [multi_mesh], targets, True
)
top_capped = max(len(v.groups) for c in capped for v in c.data.vertices)
rig_loose, loose, _, _ = op_cls._mobile_copies(
    op_cls, bpy.context, rig, [multi_mesh], targets, False
)
top_loose = max(len(v.groups) for c in loose for v in c.data.vertices)
for c in capped + loose:
    data = c.data
    bpy.data.objects.remove(c, do_unlink=True)
    if data.users == 0:
        bpy.data.meshes.remove(data)
for r in (rig_capped, rig_loose):
    data = r.data
    bpy.data.objects.remove(r, do_unlink=True)
    if data.users == 0:
        bpy.data.armatures.remove(data)
if top_capped != 4 or capped_n != 8:
    fail(f"limit toggle did not cap (max={top_capped}, n={capped_n})")
if top_loose != 5:
    fail(f"limit off should keep 5 influences (got {top_loose})")
bpy.data.objects.remove(multi_mesh, do_unlink=True)
print("validate: limit toggle caps 5 -> 4 (off keeps 5)")

# 6. Round-trip: re-import and verify the armature + skinned mesh.
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
    for x in list(coll):
        coll.remove(x)
bpy.ops.import_scene.gltf(filepath=GLB_PATH)


def is_imported(obj):
    return all(c.name != IMPORTER_HELPER_COLLECTION for c in obj.users_collection)


imported_arms = [o for o in bpy.data.objects if o.type == "ARMATURE" and is_imported(o)]
imported_meshes = [o for o in bpy.data.objects if o.type == "MESH" and is_imported(o)]
if len(imported_arms) != 1:
    fail(f"expected 1 armature on re-import, got {len(imported_arms)}")
if len(imported_meshes) != 1:
    fail(f"expected 1 mesh on re-import, got {len(imported_meshes)}")

imported_bones = [b.name for b in imported_arms[0].data.bones]
non_def = [n for n in imported_bones if not n.startswith("DEF-")]
print(
    f"re-import: {len(imported_bones)} bones, "
    f"mesh={imported_meshes[0].name}, non-DEF={len(non_def)}"
)
if non_def:
    fail(f"non-DEF bones leaked into export: {non_def[:10]}")
if len(imported_bones) != len(def_bones):
    fail(
        f"re-import has {len(imported_bones)} bones, rig has {len(def_bones)} DEF bones"
    )

# 6b. Mobile round-trip: jaw weights land on the merge target.
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
    for x in list(coll):
        coll.remove(x)
bpy.ops.import_scene.gltf(filepath=mobile_path)
mob_arms = [o for o in bpy.data.objects if o.type == "ARMATURE" and is_imported(o)]
mob_meshes = [o for o in bpy.data.objects if o.type == "MESH" and is_imported(o)]
if len(mob_arms) != 1 or len(mob_meshes) != 2:
    fail(
        f"mobile re-import: {len(mob_arms)} armatures, "
        f"{len(mob_meshes)} meshes (want 1 + 2)"
    )
if {b.name for b in mob_arms[0].data.bones} != set(kept):
    fail("mobile re-import bones != kept set")
found_target = False
for mesh in mob_meshes:
    names = [g.name for g in mesh.vertex_groups]
    if any(n in targets for n in names):
        fail(f"dropped group survived mobile export: {names}")
    if jaw_target in names:
        found_target = True
        for v in mesh.data.vertices:
            if abs(mesh.vertex_groups[jaw_target].weight(v.index) - 1.0) > 1e-4:
                fail("jaw weight did not remap to 1.0 on the target")
if not found_target:
    fail(f"merge target {jaw_target} missing from re-imported meshes")
print("mobile re-import: dropped groups gone, jaw weights on target")

# 7. Godot import check (skipped when Godot is absent).
godot_bin = os.environ.get("GODOT_BIN", "/Applications/Godot.app/Contents/MacOS/Godot")
if not (os.path.isfile(godot_bin) and os.access(godot_bin, os.X_OK)):
    print("godot: skipped (no Godot binary)")
else:
    proc = subprocess.run(
        [
            sys.executable,
            "tools/godot_import_check.py",
            GLB_PATH,
            mobile_path,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        fail(f"godot import check failed: {proc.stdout} {proc.stderr[-500:]}")
    bones_by_path = {}
    for line in proc.stdout.splitlines():
        if "GODOT_STAT" not in line:
            continue
        parts = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
        bones_by_path[parts["path"]] = int(parts["bones"])
    if bones_by_path.get(GLB_PATH) != len(def_bones):
        fail(f"godot full bones wrong: {bones_by_path}")
    if bones_by_path.get(mobile_path) != len(kept):
        fail(f"godot mobile bones wrong: {bones_by_path}")
    print("godot: both GLBs import with full/mobile skeletons")

print("RIGFORGE_EXPORT_TEST_OK")
