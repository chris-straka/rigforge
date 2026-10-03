# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: nudge strokes solver + wm.rigforge_nudge (W2).

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_nudge.py

Gates: pins within 2% (soft springs: exact pins would snap
neighbors into creases), excludes under 2%, refill smooth (no
neighbor cliffs), influences <= 4, bit-identical re-solve, operator
strokes + clear + errors + undo-flag, damaged-elbow repair improves the
bend, full re-solve under 1 s on a 15k-tri mesh.

Pass criteria: exits 0 and prints RIGFORGE_NUDGE_OK.
"""

import os
import sys
import time

sys.path.insert(0, "tests")
os.environ["RF_W1_ONLY"] = "none"

import bmesh
import bpy
import numpy as np


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


bpy.ops.preferences.addon_enable(module="rigforge")
import test_weights_w1 as T  # noqa: E402

import rigforge  # noqa: E402
from rigforge.operators import nudge as nudge_op  # noqa: E402
from rigforge.weights import nudge as nudge_mod  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_nudge"), "operator not registered")


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


# --- Solver: pins, realistic excludes, smooth refill, deterministic ---
mesh, rig = T.build_tube()
T.voxel_bind(mesh, rig)
z_of = np.array([tuple(v.co) for v in mesh.data.vertices])[:, 2]
pin_rows = np.flatnonzero(z_of < 0.15).tolist()
# Realistic exclude: the blend band, where the bone is minor-to-mid
# (reassigning a blend that favors Bone.001 too much).
exc_rows = np.flatnonzero((z_of > 0.20) & (z_of < 0.30)).tolist()
pins = {vi: {"Bone": 1.0} for vi in pin_rows}
pins.update({vi: {"Bone.001": None} for vi in exc_rows})
per_vert, _names = nudge_mod.read_weights(mesh)
offsets, neighbors = nudge_mod.mesh_adjacency(mesh)
solved = nudge_mod.solve_nudge(per_vert, offsets, neighbors, pins)
solved2 = nudge_mod.solve_nudge(per_vert, offsets, neighbors, pins)
check(solved == solved2, "re-solve differs (not deterministic)")
nudge_mod.apply_nudge(mesh, solved)
w_bone = weights_of(mesh, "Bone")
w_b001 = weights_of(mesh, "Bone.001")
check(
    all(abs(w_bone.get(vi, 0.0) - 1.0) < 0.02 for vi in pin_rows),
    "pinned verts not within 2% of 1.0",
)
check(
    all(w_b001.get(vi, 0.0) < 0.02 for vi in exc_rows),
    "excluded bone not under 2%",
)
jump = max_neighbor_jump(mesh)
check(jump < 0.5, f"refill cliff between neighbors: {jump}")
kmax = max(len(v.groups) for v in mesh.data.vertices)
check(kmax <= 4, f"influence budget blown: {kmax}")
print(f"solver: pins ~exact, excludes ~0, max neighbor jump {jump:.3f}, k<={kmax}")

# --- Solver: pathological excludes stay bounded (reassignment cliff) ---
# Excluding a dominant bone over a whole region forces a crisp
# reassignment in any solver (sum-to-1 leaves no alternative).
# Bounded (finite, normalized, budgeted), not smooth.
wild = {vi: {"Bone.001": None} for vi in np.flatnonzero(z_of > 0.35).tolist()}
wild_solved = nudge_mod.solve_nudge(per_vert, offsets, neighbors, wild)
for pairs in wild_solved:
    for _b, w in pairs:
        check(np.isfinite(w), "non-finite weight from pathological exclude")
    if pairs:
        check(abs(sum(w for _, w in pairs) - 1.0) < 1e-6, "weights not normalized")
check(max(len(p) for p in wild_solved) <= 4, "budget blown on wild solve")
print("solver: pathological excludes bounded")

# --- Operator: strokes + clear + errors ---
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
mesh.select_set(True)
bpy.context.view_layer.objects.active = mesh
for v in mesh.data.vertices:
    v.select = v.index in pin_rows
# A locked non-bone group rides along untouched through every stroke.
mask = mesh.vertex_groups.new(name="Mask")
mask.add(range(len(mesh.data.vertices)), 0.5, "REPLACE")
mask.lock_weight = True
group_order = [g.name for g in mesh.vertex_groups]
before = weights_of(mesh, "Bone")
result = bpy.ops.wm.rigforge_nudge(bone="Bone", value=0.7, mode="PIN")
check("FINISHED" in result, f"pin did not finish: {result}")
after = weights_of(mesh, "Bone")
check(
    all(abs(after.get(vi, 0.0) - 0.7) < 0.02 for vi in pin_rows),
    "operator pin value not honored",
)
near = np.flatnonzero((z_of > 0.15) & (z_of < 0.20)).tolist()
check(
    any(abs(after.get(vi, 0) - before.get(vi, 0)) > 1e-9 for vi in near),
    "pin did not refill outside the pinned rows",
)
stored = nudge_op.get_pins(mesh)
check(len(stored) == len(pin_rows), f"pins not stored: {len(stored)}")
for v in mesh.data.vertices:
    v.select = v.index in exc_rows
result = bpy.ops.wm.rigforge_nudge(bone="Bone.001", mode="EXCLUDE")
check("FINISHED" in result, f"exclude did not finish: {result}")
w_after = weights_of(mesh, "Bone.001")
check(
    all(w_after.get(vi, 0.0) < 0.02 for vi in exc_rows),
    "operator exclude not under 2%",
)
check(
    [g.name for g in mesh.vertex_groups] == group_order,
    f"strokes reordered groups: {[g.name for g in mesh.vertex_groups]}",
)
check(mesh.vertex_groups["Mask"].lock_weight, "strokes dropped the Mask lock")
check(
    weights_of(mesh, "Mask") == dict.fromkeys(range(len(mesh.data.vertices)), 0.5),
    "strokes rewrote the Mask group",
)
bone_rows = nudge_mod.read_weights(mesh, {"Bone", "Bone.001"})[0]
check(
    all(abs(sum(r.values()) - 1.0) < 1e-5 for r in bone_rows),
    "bone rows not normalized (Mask solved as a bone?)",
)
for v in mesh.data.vertices:
    v.select = v.index in pin_rows
try:
    result = bpy.ops.wm.rigforge_nudge(bone="Mask", value=0.7, mode="PIN")
except RuntimeError as exc:
    result = str(exc)
check("not a deform bone" in str(result), f"pinning a mask gave {result}")
mesh.vertex_groups.remove(mesh.vertex_groups["Mask"])
result = bpy.ops.wm.rigforge_nudge(mode="CLEAR")
check("FINISHED" in result, f"clear did not finish: {result}")
check("rigforge_nudge_pins" not in mesh, "pins not cleared")
print("operator: pin/exclude/clear OK (Mask group untouched)")


def expect_cancelled(fn, needle):
    try:
        result = fn()
    except RuntimeError as exc:
        check(needle in str(exc), f"wrong error: {exc}")
        return
    check("CANCELLED" in result, f"expected cancel, got {result}")


for v in mesh.data.vertices:
    v.select = False
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_nudge(bone="Bone", mode="PIN"), "Select verts"
)
for v in mesh.data.vertices:
    v.select = v.index in pin_rows
expect_cancelled(lambda: bpy.ops.wm.rigforge_nudge(bone="", mode="PIN"), "Pick a bone")
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_nudge(bone="Nope", mode="PIN"), "no vertex group"
)
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
check(
    bpy.ops.wm.rigforge_nudge.poll() is False,
    "operator should not poll on an armature",
)
print("operator: error paths OK")

# --- Undo: one stroke == one undo step (static: background Blender has
# no undo context, so assert registration, not behavior) ---
check(
    "UNDO" in nudge_op.WM_OT_rigforge_nudge.bl_options,
    "operator lost its UNDO flag (strokes would not be undoable)",
)
bpy.ops.object.select_all(action="DESELECT")
mesh.select_set(True)
bpy.context.view_layer.objects.active = mesh
for v in mesh.data.vertices:
    v.select = v.index in pin_rows
snap = weights_of(mesh, "Bone")
bpy.ops.wm.rigforge_nudge(bone="Bone", value=0.2, mode="PIN")
check(weights_of(mesh, "Bone") != snap, "stroke changed nothing")
print("operator: undo registered, stroke mutates")

# --- Repair: damaged blend bends badly, nudged blend bends better ---
mesh_d, rig_d = T.build_tube()
T.voxel_bind(mesh_d, rig_d)
T.set_fk_mode(rig_d)
zd = np.array([tuple(v.co) for v in mesh_d.data.vertices])[:, 2]
mid = np.flatnonzero((zd > 0.15) & (zd < 0.35)).tolist()
gb = mesh_d.vertex_groups.find("Bone")
gb1 = mesh_d.vertex_groups.find("Bone.001")
rng = np.random.default_rng(3)
for vi in mid:
    w = float(rng.uniform(0.2, 0.8))
    mesh_d.vertex_groups[gb].add([vi], w, "REPLACE")
    mesh_d.vertex_groups[gb1].add([vi], 1.0 - w, "REPLACE")
tris_d = T.mesh_tris(mesh_d)
region_d = T.joint_region(mesh_d, ["Bone", "Bone.001"])
rest_d = T.evaluated_positions(mesh_d)
pose = T.load_spec("tube")["poses"]["elbow_90"]
touched = T.apply_pose(rig_d, pose)
bad = T.measure("repair/bad", rest_d, T.evaluated_positions(mesh_d), tris_d, region_d)
T.clear_bones(rig_d, touched)
above = np.flatnonzero(zd >= 0.35).tolist()
below = np.flatnonzero(zd <= 0.15).tolist()
fix = {vi: {"Bone.001": 1.0} for vi in above}
fix.update({vi: {"Bone": 1.0} for vi in below})
per_d, _ = nudge_mod.read_weights(mesh_d)
off_d, nbr_d = nudge_mod.mesh_adjacency(mesh_d)
nudge_mod.apply_nudge(mesh_d, nudge_mod.solve_nudge(per_d, off_d, nbr_d, fix))
rest_d2 = T.evaluated_positions(mesh_d)
touched = T.apply_pose(rig_d, pose)
good = T.measure(
    "repair/good", rest_d2, T.evaluated_positions(mesh_d), tris_d, region_d
)
T.clear_bones(rig_d, touched)
check(
    good["flips"] < bad["flips"] or good["p95"] < bad["p95"],
    f"nudge did not repair: bad={bad['p95']:.3f}/{bad['flips']} "
    f"good={good['p95']:.3f}/{good['flips']}",
)
print(
    f"repair: p95 {bad['p95']:.3f}->{good['p95']:.3f}, "
    f"flips {bad['flips']}->{good['flips']}"
)

# --- Perf: full re-solve under 1 s on a 15k-tri mesh ---
mesh_p = bpy.data.objects.new("Perf", bpy.data.meshes.new("Perf"))
bpy.context.scene.collection.objects.link(mesh_p)
bm = bmesh.new()
n_around, n_height = 64, 120  # 64*120*2 = 15360 tris (the real 15k budget)
rings = []
for j in range(n_height + 1):
    z = 0.5 * j / n_height
    rings.append(
        [
            bm.verts.new(
                (
                    0.05 * np.cos(2 * np.pi * i / n_around),
                    0.05 * np.sin(2 * np.pi * i / n_around),
                    z,
                )
            )
            for i in range(n_around)
        ]
    )
for j in range(n_height):
    for i in range(n_around):
        bm.faces.new(
            (
                rings[j][i],
                rings[j][(i + 1) % n_around],
                rings[j + 1][(i + 1) % n_around],
                rings[j + 1][i],
            )
        )
bm.to_mesh(mesh_p.data)
bm.free()
mesh_p.data.calc_loop_triangles()
ntri = len(mesh_p.data.loop_triangles)
ga = mesh_p.vertex_groups.new(name="A")
gb = mesh_p.vertex_groups.new(name="B")
for v in mesh_p.data.vertices:
    w = v.co.z / 0.5
    ga.add([v.index], 1.0 - w, "REPLACE")
    gb.add([v.index], w, "REPLACE")
per_p, _ = nudge_mod.read_weights(mesh_p)
off_p, nbr_p = nudge_mod.mesh_adjacency(mesh_p)
zp = np.array([tuple(v.co) for v in mesh_p.data.vertices])[:, 2]
pins_p = {0: {"A": 1.0}, len(zp) - 1: {"B": 1.0}}
t0 = time.perf_counter()
nudge_mod.solve_nudge(per_p, off_p, nbr_p, pins_p)
dt = time.perf_counter() - t0
check(dt < 1.0, f"re-solve took {dt:.2f} s on {ntri} tris (>= 1 s)")
print(f"perf: re-solve {dt:.3f} s on {ntri} tris")

print("RIGFORGE_NUDGE_OK")
