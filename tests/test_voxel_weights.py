# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: wm.rigforge_voxel_weights on tube + open mesh.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_voxel_weights.py

Pass criteria: exits 0 and prints RIGFORGE_VOXEL_WEIGHTS_OK.
"""

import os
import sys

sys.path.insert(0, "tests")
os.environ["RF_W1_ONLY"] = "none"

import bpy


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def fail(msg):
    raise AssertionError(msg)


bpy.ops.preferences.addon_enable(module="rigforge")
import test_weights_w1 as T  # noqa: E402

import rigforge  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_voxel_weights"), "operator not registered")


def select_for_bind(mesh, rig):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = mesh


def weight_snapshot(mesh):
    groups = mesh.vertex_groups
    snap = {}
    for v in mesh.data.vertices:
        snap[v.index] = sorted(
            (groups[e.group].name, round(e.weight, 9)) for e in v.groups
        )
    return snap


def check_bound(mesh, label):
    check(len(mesh.vertex_groups) > 0, f"{label}: no vertex groups")
    bare = [v.index for v in mesh.data.vertices if len(v.groups) == 0]
    check(not bare, f"{label}: {len(bare)} verts with no groups")
    mods = [m for m in mesh.modifiers if m.type == "ARMATURE"]
    check(mods, f"{label}: no Armature modifier")


# --- Tube: bind + contents + determinism ---
mesh, rig = T.build_tube()
select_for_bind(mesh, rig)
result = bpy.ops.wm.rigforge_voxel_weights()
check("FINISHED" in result, f"tube operator did not finish: {result}")
check_bound(mesh, "tube")
names = sorted(g.name for g in mesh.vertex_groups)
check(names == ["Bone", "Bone.001"], f"tube groups: {names}")
first = weight_snapshot(mesh)
print(f"tube bound: {len(names)} groups, {len(first)} verts weighted")

dup = T.duplicate_mesh(mesh, "tube_redo")
select_for_bind(dup, rig)
result = bpy.ops.wm.rigforge_voxel_weights()
check("FINISHED" in result, f"redo operator did not finish: {result}")
second = weight_snapshot(dup)
check(first == second, "second bind differs (not deterministic)")
print("tube deterministic: re-bind identical")

# --- Stacked shells + envelope flag: fused exterior stands in ---
mesh2, rig2 = T.build_tube()
import bmesh  # noqa: E402

bm = bmesh.new()
bm.from_mesh(mesh2.data)
vmap = {v: bm.verts.new((v.co.x * 1.06, v.co.y * 1.06, v.co.z)) for v in list(bm.verts)}
for f in list(bm.faces):
    bm.faces.new([vmap[v] for v in f.verts])
bm.to_mesh(mesh2.data)
bm.free()
select_for_bind(mesh2, rig2)
result = bpy.ops.wm.rigforge_voxel_weights(use_envelope=True)
check("FINISHED" in result, f"envelope operator did not finish: {result}")
check_bound(mesh2, "stacked+envelope")
leftovers = [o.name for o in bpy.data.objects if o.name.startswith("Tube.")]
check(not leftovers, f"envelope temps left behind: {leftovers}")
print("stacked shells + envelope: bound OK, no temps left")


# --- Error paths ---
def expect_cancelled(fn, needle):
    try:
        result = fn()
    except RuntimeError as exc:
        check(needle in str(exc), f"wrong error: {exc}")
        return
    check("CANCELLED" in result, f"expected cancel, got {result}")


mesh3, rig3 = T.build_tube()
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
mesh3.select_set(True)
bpy.context.view_layer.objects.active = mesh3
expect_cancelled(bpy.ops.wm.rigforge_voxel_weights, "No rig found")

# Open mesh: parity leaks, no interior — honest cancel, not garbage.
bm = bmesh.new()
bm.from_mesh(mesh3.data)
for f in list(bm.faces):
    if len(f.verts) > 4:
        bm.faces.remove(f)
bm.to_mesh(mesh3.data)
bm.free()
select_for_bind(mesh3, rig3)
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_voxel_weights(use_envelope=True), "not closed"
)

bpy.ops.object.select_all(action="DESELECT")
rig3.select_set(True)
bpy.context.view_layer.objects.active = rig3
check(
    bpy.ops.wm.rigforge_voxel_weights.poll() is False,
    "operator should not poll on an armature",
)

print("RIGFORGE_VOXEL_WEIGHTS_OK")
