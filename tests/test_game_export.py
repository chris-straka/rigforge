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

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
print("enable OK:", rigforge.__file__)

# 2. Human metarig -> generate.
bpy.ops.object.armature_human_metarig_add()
bpy.ops.object.mode_set(mode="OBJECT")
if bpy.ops.wm.rigforge_game_export.poll():
    fail("export poll should be False on a metarig")
bpy.ops.pose.rigforge_generate()
print("generate OK")

rig = next(
    (o for o in bpy.data.objects
     if o.type == 'ARMATURE' and 'rig_id' in o.data),
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
mod = mesh_obj.modifiers.new("Armature", 'ARMATURE')
mod.object = rig
vg = mesh_obj.vertex_groups.new(name=def_bones[0])
vg.add(range(len(mesh_obj.data.vertices)), 1.0, 'REPLACE')
print(f"mesh: {mesh_obj.name} bound to {def_bones[0]}")

# 4. Run the one-click export operator.
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action='DESELECT')
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
with open(skel_path, 'rb') as f:
    skel_raw = f.read()
skel_json_len = struct.unpack('<I', skel_raw[12:16])[0]
skel = json.loads(skel_raw[20:20 + skel_json_len])
skel_joints = sum(len(s['joints']) for s in skel.get('skins', []))
print(f"skeleton-only: meshes={skel.get('meshes', [])} joints={skel_joints}")
if skel.get('meshes'):
    fail(f"skeleton-only export should have no meshes: {skel['meshes']}")
if skel_joints != len(def_bones):
    fail(f"skeleton-only export has {skel_joints} joints, "
         f"expected {len(def_bones)}")

# 5. Independent check: parse the GLB JSON chunk directly (no importer).
with open(GLB_PATH, 'rb') as f:
    raw = f.read()
json_len = struct.unpack('<I', raw[12:16])[0]
gltf = json.loads(raw[20:20 + json_len])

mesh_names = [m.get('name') for m in gltf.get('meshes', [])]
joint_names = []
for skin in gltf.get('skins', []):
    joint_names += [gltf['nodes'][j].get('name') for j in skin['joints']]
print(f"glb json: meshes={mesh_names} joints={len(joint_names)}")
if mesh_names != [mesh_name]:
    fail(f"expected exactly the skinned mesh in the GLB, got {mesh_names}")
if not joint_names:
    fail("GLB skin has no joints")
non_def_joints = [n for n in joint_names if not (n or '').startswith("DEF-")]
if non_def_joints:
    fail(f"non-DEF joints in GLB: {non_def_joints[:10]}")
if len(joint_names) != len(def_bones):
    fail(f"GLB has {len(joint_names)} joints, rig has {len(def_bones)} DEF bones")

# 6. Round-trip: re-import and verify the armature + skinned mesh.
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
    for x in list(coll):
        coll.remove(x)
bpy.ops.import_scene.gltf(filepath=GLB_PATH)


def is_imported(obj):
    return all(c.name != IMPORTER_HELPER_COLLECTION
               for c in obj.users_collection)


imported_arms = [o for o in bpy.data.objects
                 if o.type == 'ARMATURE' and is_imported(o)]
imported_meshes = [o for o in bpy.data.objects
                   if o.type == 'MESH' and is_imported(o)]
if len(imported_arms) != 1:
    fail(f"expected 1 armature on re-import, got {len(imported_arms)}")
if len(imported_meshes) != 1:
    fail(f"expected 1 mesh on re-import, got {len(imported_meshes)}")

imported_bones = [b.name for b in imported_arms[0].data.bones]
non_def = [n for n in imported_bones if not n.startswith("DEF-")]
print(f"re-import: {len(imported_bones)} bones, "
      f"mesh={imported_meshes[0].name}, non-DEF={len(non_def)}")
if non_def:
    fail(f"non-DEF bones leaked into export: {non_def[:10]}")
if len(imported_bones) != len(def_bones):
    fail(f"re-import has {len(imported_bones)} bones, "
         f"rig has {len(def_bones)} DEF bones")

print("RIGFORGE_EXPORT_TEST_OK")
