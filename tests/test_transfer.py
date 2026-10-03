# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: piece transfer solver + wm.rigforge_piece_transfer (W3).

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_transfer.py

Fixture: a cape sheet pinned near the upper tube (Bone.001 country)
hanging down past the elbow, hem nearest the lower tube (Bone
country). Plain nearest-copy pulls the hem through to Bone; the
transfer must inpaint Bone.001 down the sheet instead.

Gates: close verts copy the body, far verts inpaint (hem stays
Bone.001, naive baseline demonstrably pulls through), refill smooth,
influences <= 4, bit-identical re-transfer, all-far fallback seeds,
elbow bend rides the hem rigidly on the bone (no explosion), operator +
errors + undo-flag + armature-modifier copy.

Pass criteria: exits 0 and prints RIGFORGE_TRANSFER_OK.
"""

import os
import sys

sys.path.insert(0, "tests")
os.environ["RF_W1_ONLY"] = "none"

import bmesh
import bpy
import numpy as np
from mathutils import Vector


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


bpy.ops.preferences.addon_enable(module="rigforge")
import test_weights_w1 as T  # noqa: E402

import rigforge  # noqa: E402
from rigforge.operators import transfer as transfer_op  # noqa: E402
from rigforge.weights import nudge as nudge_mod  # noqa: E402
from rigforge.weights import transfer as transfer_mod  # noqa: E402
from rigforge.weights import voxel as voxel_mod  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_piece_transfer"), "operator not registered")


def weights_of(mesh, bone):
    g = mesh.vertex_groups.find(bone)
    out = {}
    for v in mesh.data.vertices:
        for el in v.groups:
            if el.group == g:
                out[v.index] = el.weight
    return out


def max_neighbor_jump(mesh):
    groups = mesh.vertex_groups
    names = [g.name for g in groups]
    idx = {n: i for i, n in enumerate(names)}
    mat = np.zeros((len(mesh.data.vertices), len(names)))
    for v in mesh.data.vertices:
        for el in v.groups:
            mat[v.index, idx[groups[el.group].name]] = el.weight
    edges = np.array(
        [(e.vertices[0], e.vertices[1]) for e in mesh.data.edges], dtype=np.int64
    )
    return float(np.abs(mat[edges[:, 0]] - mat[edges[:, 1]]).sum(axis=1).max())


N_COLS, N_ROWS = 8, 12


def build_cape():
    data = bpy.data.meshes.new("Cape")
    bm = bmesh.new()
    grid = []
    for j in range(N_ROWS):
        t = j / (N_ROWS - 1)
        x = 0.055 + 0.16 * t**1.5
        z = 0.38 - 0.32 * t
        grid.append(
            [
                bm.verts.new((x, -0.06 + 0.12 * i / (N_COLS - 1), z))
                for i in range(N_COLS)
            ]
        )
    for j in range(N_ROWS - 1):
        for i in range(N_COLS - 1):
            bm.faces.new(
                (grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i])
            )
    bm.to_mesh(data)
    bm.free()
    cape = bpy.data.objects.new("Cape", data)
    bpy.context.scene.collection.objects.link(cape)
    return cape


def row_of(cape):
    return [v.index // N_COLS for v in cape.data.vertices]


# --- Solver: naive baseline pulls through, transfer inpaints ---
mesh, rig = T.build_tube()
T.voxel_bind(mesh, rig)
cape = build_cape()
rows = row_of(cape)
top = [vi for vi, j in enumerate(rows) if j <= 1]
hem = [vi for vi, j in enumerate(rows) if j >= N_ROWS - 2]

naive, _ = transfer_mod.solve_transfer(mesh, cape, inpaint=False)
nudge_mod.apply_nudge(cape, naive)
w_b, w_b1 = weights_of(cape, "Bone"), weights_of(cape, "Bone.001")
check(
    all(w_b.get(vi, 0.0) > 0.8 for vi in hem),
    "fixture does not discriminate (naive hem should pull to Bone)",
)
print("solver: naive baseline pulls the hem through to Bone (as expected)")

fixed, report = transfer_mod.solve_transfer(mesh, cape)
check(report["close"] >= 10, f"too few close verts: {report}")
check(report["far"] >= 50, f"too few far verts: {report}")
check(not report["seeded"], "fallback seeded but close verts exist")
fixed2, _ = transfer_mod.solve_transfer(mesh, cape)
check(fixed == fixed2, "re-transfer differs (not deterministic)")
nudge_mod.apply_nudge(cape, fixed)
w_b, w_b1 = weights_of(cape, "Bone"), weights_of(cape, "Bone.001")
check(
    all(w_b1.get(vi, 0.0) > 0.9 for vi in top),
    "close verts did not copy the body",
)
check(
    all(w_b1.get(vi, 0.0) > 0.8 for vi in hem),
    "hem pulled through (inpaint failed)",
)
check(
    all(w_b.get(vi, 0.0) < 0.2 for vi in hem),
    "hem carries lower-arm weight",
)
jump = max_neighbor_jump(cape)
check(jump < 0.5, f"transfer cliff between neighbors: {jump}")
kmax = max(len(v.groups) for v in cape.data.vertices)
check(kmax <= 4, f"influence budget blown: {kmax}")
print(
    f"solver: {report['close']} close + {report['far']} inpainted, "
    f"hem Bone.001, max neighbor jump {jump:.3f}, k<={kmax}"
)

# --- Solver: all-far fallback seeds the nearest vert ---
cape.location.x = 2.0
bpy.context.view_layer.update()
seeded, rep = transfer_mod.solve_transfer(mesh, cape)
check(rep["seeded"], "all-far piece did not seed")
nudge_mod.apply_nudge(cape, seeded)
w_b1 = weights_of(cape, "Bone.001")
check(all(w_b1.get(vi, 0.0) > 0.8 for vi in hem), "seeded fallback not uniform")
cape.location.x = 0.0
bpy.context.view_layer.update()
print("solver: all-far fallback seeds + inpaints uniformly")
nudge_mod.apply_nudge(cape, fixed)

# --- Bend: the hem rides Bone.001 rigidly (no pull-through, no explosion) ---
voxel_mod.ensure_armature_modifier(cape, rig)
T.set_fk_mode(rig)
rest = T.evaluated_positions(cape)
bone = rig.pose.bones["Bone.001"]
mat_rest = rig.matrix_world @ bone.matrix
pose = T.load_spec("tube")["poses"]["elbow_90"]
touched = T.apply_pose(rig, pose)
mat_posed = rig.matrix_world @ bone.matrix
posed = T.evaluated_positions(cape)
T.clear_bones(rig, touched)
check(np.all(np.isfinite(posed)), "non-finite cape displacement")
rigid = mat_posed @ mat_rest.inverted()
rest_w = np.array([tuple(cape.matrix_world @ v.co) for v in cape.data.vertices])
posed_w = np.array([tuple(cape.matrix_world @ Vector(p)) for p in posed])
pred = np.array([tuple(rigid @ Vector(p)) for p in rest_w])
hem_err = float(np.abs(pred[hem] - posed_w[hem]).max())
check(hem_err < 0.002, f"hem does not ride the bone: err={hem_err:.4f}")
check(float(np.abs(posed_w - rest_w).max()) < 1.0, "cape displacement exploded")
print(f"bend: hem rides Bone.001 rigidly (err={hem_err:.5f}), no explosion")

# --- Operator: transfer + modifier + errors ---
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
cape.select_set(True)
bpy.context.view_layer.objects.active = cape
# Non-bone groups: the body's Mask is not a weight to hand on, the
# cape's own Pin group must survive the replace.
body_mask = mesh.vertex_groups.new(name="Mask")
body_mask.add(range(len(mesh.data.vertices)), 0.7, "REPLACE")
pin = cape.vertex_groups.new(name="Pin")
pin.add(range(len(cape.data.vertices)), 0.4, "REPLACE")
result = bpy.ops.wm.rigforge_piece_transfer(source="Tube")
check("FINISHED" in result, f"transfer did not finish: {result}")
check("Bone.001" in cape.vertex_groups, "transfer wrote no groups")
check("Mask" not in cape.vertex_groups, "body Mask leaked onto the cape")
check(
    all(
        abs(cape.vertex_groups["Pin"].weight(v.index) - 0.4) < 1e-6
        for v in cape.data.vertices
    ),
    "transfer rewrote the cape's Pin group",
)
cape_rows = nudge_mod.read_weights(cape, {"Bone", "Bone.001"})[0]
check(
    all(abs(sum(r.values()) - 1.0) < 1e-5 for r in cape_rows),
    "cape bone rows not normalized",
)
mesh.vertex_groups.remove(body_mask)
check(
    any(m.type == "ARMATURE" and m.object == rig for m in cape.modifiers),
    "armature modifier not copied",
)
print("operator: transfer + modifier copy OK")


def expect_cancelled(fn, needle):
    try:
        result = fn()
    except RuntimeError as exc:
        check(needle in str(exc), f"wrong error: {exc}")
        return
    check("CANCELLED" in result, f"expected cancel, got {result}")


expect_cancelled(lambda: bpy.ops.wm.rigforge_piece_transfer(source=""), "Pick a source")
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_piece_transfer(source="Nope"), "not a mesh"
)
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_piece_transfer(source="Cape"), "must differ"
)
empty_data = bpy.data.meshes.new("EmptyBody")
empty = bpy.data.objects.new("EmptyBody", empty_data)
bpy.context.scene.collection.objects.link(empty)
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_piece_transfer(source="EmptyBody"), "no weights"
)
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
check(
    bpy.ops.wm.rigforge_piece_transfer.poll() is False,
    "operator should not poll on an armature",
)
check(
    "UNDO" in transfer_op.WM_OT_rigforge_piece_transfer.bl_options,
    "operator lost its UNDO flag",
)
print("operator: error paths + undo-flag OK")

print("RIGFORGE_TRANSFER_OK")
