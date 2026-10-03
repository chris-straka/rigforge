# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: cleanup rows + wm.rigforge_cleanup (W4).

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_cleanup.py

Gates: limit keeps top-k (ties by name) renormalized, normalize
scales, prune drops specks (stranded rows keep max), smooth diffuses
spikes over fixed passes, mirror swaps L<->R across X, everything
deterministic, 6-bone paint limits to the 4-influence mobile budget,
operator + errors + undo-flag.

Pass criteria: exits 0 and prints RIGFORGE_CLEANUP_OK.
"""

import os
import sys

sys.path.insert(0, "tests")
os.environ["RF_W1_ONLY"] = "none"

import bpy


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


def approx(a, b, tol=1e-9):
    return abs(a - b) < tol


bpy.ops.preferences.addon_enable(module="rigforge")
import test_weights_w1 as T  # noqa: E402

import rigforge  # noqa: E402
from rigforge.operators import cleanup as cleanup_op  # noqa: E402
from rigforge.weights import cleanup as cleanup_mod  # noqa: E402
from rigforge.weights import nudge as nudge_mod  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_cleanup"), "operator not registered")


def rows_of(mesh):
    rows, _ = nudge_mod.read_weights(mesh)
    return rows


def paint(mesh, fn):
    mesh.vertex_groups.clear()
    cache = {}

    def group(name):
        if name not in cache:
            cache[name] = mesh.vertex_groups.new(name=name)
        return cache[name]

    for v in mesh.data.vertices:
        for name, w in fn(v).items():
            group(name).add([v.index], w, "REPLACE")


# --- Pure rows: limit / normalize / prune ---
limited = cleanup_mod.limit_rows([{"e": 1, "d": 2, "c": 3, "b": 4, "a": 5}], k=4)[0]
check(set(limited) == {"a", "b", "c", "d"}, f"limit kept {sorted(limited)}")
check(approx(sum(limited.values()), 1.0), "limit not normalized")
check(approx(limited["a"], 5 / 14), "limit skewed values")
tied = cleanup_mod.limit_rows([{"b": 1, "a": 1, "c": 1}], k=2)[0]
check(set(tied) == {"a", "b"}, f"limit tiebreak unstable: {sorted(tied)}")
check(cleanup_mod.limit_rows([{"a": 2}], k=4) == [{"a": 1.0}], "limit of one")
check(cleanup_mod.limit_rows([{}], k=4) == [{}], "limit of empty")
normed = cleanup_mod.normalize_rows([{"a": 2, "b": 2}, {}])
check(normed == [{"a": 0.5, "b": 0.5}, {}], f"normalize gave {normed}")
pruned = cleanup_mod.prune_rows([{"a": 0.6, "b": 0.3, "c": 0.05}], threshold=0.1)[0]
check(set(pruned) == {"a", "b"}, f"prune kept {sorted(pruned)}")
check(approx(pruned["a"], 0.6 / 0.9), "prune skewed values")
stranded = cleanup_mod.prune_rows([{"a": 0.04, "b": 0.03}], threshold=0.1)[0]
check(stranded == {"a": 1.0}, f"stranded row gave {stranded}")
tie_strand = cleanup_mod.prune_rows([{"b": 0.03, "a": 0.03}], threshold=0.1)[0]
check(tie_strand == {"a": 1.0}, f"stranded tiebreak gave {tie_strand}")
check(cleanup_mod.prune_rows([{}], threshold=0.1) == [{}], "prune of empty")
check(cleanup_mod.mirror_bone("DEF-arm.L") == "DEF-arm.R", "mirror .L")
check(cleanup_mod.mirror_bone("DEF-arm.R") == "DEF-arm.L", "mirror .R")
check(cleanup_mod.mirror_bone("leg_L") == "leg_R", "mirror _L")
check(cleanup_mod.mirror_bone("spine") == "spine", "mirror unmarked")
# Rigify puts the side before the .NNN number (twist + face DEF bones).
check(
    cleanup_mod.mirror_bone("DEF-upper_arm.L.001") == "DEF-upper_arm.R.001",
    "mirror twist .L.001",
)
check(
    cleanup_mod.mirror_bone("DEF-lip.T.L.001") == "DEF-lip.T.R.001",
    "mirror face .T.L.001",
)
check(cleanup_mod.mirror_bone("hand_l") == "hand_r", "mirror keeps case")
check(cleanup_mod.mirror_bone("DEF-spine.001") == "DEF-spine.001", "mirror .001")
print("rows: limit/normalize/prune/mirror-name OK")

# --- Smooth + mirror on the tube ---
mesh, rig = T.build_tube()
T.voxel_bind(mesh, rig)
n = len(mesh.data.vertices)
offsets, neighbors = nudge_mod.mesh_adjacency(mesh)
spike = [{"Bone": 1.0} for _ in range(n)]
spike[0] = {"Bone.001": 1.0}
spike[1] = {}
calm = cleanup_mod.smooth_rows(spike, offsets, neighbors, 10)
calm2 = cleanup_mod.smooth_rows(spike, offsets, neighbors, 10)
check(calm == calm2, "smooth not deterministic")
check(calm[0].get("Bone.001", 0.0) < 1.0, "spike did not diffuse")
check(
    any(calm[int(nb)].get("Bone.001", 0.0) > 0 for nb in neighbors[: offsets[1]]),
    "neighbors took none of the spike",
)
check(calm[1] == {}, "smooth filled an empty row")
check(
    all(abs(sum(r.values()) - 1.0) < 1e-9 for r in calm if r),
    "smooth rows not normalized",
)
print("rows: smooth diffuses + deterministic")

xs = [v.co.x for v in mesh.data.vertices]
synth = []
for x in xs:
    if x > 0:
        synth.append({"Arm.L": 1.0})
    else:
        synth.append({"Arm.R": 0.3, "Spine": 0.7})
mirrored = cleanup_mod.mirror_rows(synth, mesh)
check(
    cleanup_mod.mirror_rows(synth, mesh) == mirrored,
    "mirror not deterministic",
)
li = xs.index(max(xs))
ri = xs.index(min(xs))
check(mirrored[ri] == {"Arm.R": 1.0}, f"right vert gave {mirrored[ri]}")
check(
    mirrored[li] == {"Arm.L": 0.3, "Spine": 0.7},
    f"left vert gave {mirrored[li]}",
)
# Nearest-to-center vert partners with itself (2|x| ~ 1e-16 vs 0.013
# to any neighbor), so it mirrors in place whatever side it is on.
ci = min(range(n), key=lambda i: abs(xs[i]))
expect_c = {cleanup_mod.mirror_bone(b): w for b, w in synth[ci].items()}
check(mirrored[ci] == expect_c, f"center vert gave {mirrored[ci]}")
print("rows: mirror swaps L<->R + deterministic")

# --- Operator: the mobile budget + every mode ---
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
mesh.select_set(True)
bpy.context.view_layer.objects.active = mesh
# Blender clamps group weights to [0, 1]: paint in range, distinct.
paint(mesh, lambda v: {f"B{b}": (b + 1) / 6.0 for b in range(6)})
# B0..B5 are not rig bones: All Groups (the default only cleans the
# rig's deform-bone groups; covered below).
result = bpy.ops.wm.rigforge_cleanup(mode="LIMIT", limit=4, subset="ALL")
check("FINISHED" in result, f"limit did not finish: {result}")
rows = rows_of(mesh)
check(all(len(r) <= 4 for r in rows), "limit blew the mobile budget")
check(all(abs(sum(r.values()) - 1.0) < 1e-9 for r in rows), "limit denormalized")
check(all(set(r) == {"B2", "B3", "B4", "B5"} for r in rows), "limit kept wrong set")
print("operator: limit hits the 4-influence mobile budget")

paint(mesh, lambda v: {"Bone": 0.6, "Bone.001": 0.4})
check("FINISHED" in bpy.ops.wm.rigforge_cleanup(mode="PRUNE", threshold=0.5))
rows = rows_of(mesh)
check(all(set(r) == {"Bone"} for r in rows), "prune did not collapse blends")
paint(mesh, lambda v: {"Bone": 0.2, "Bone.001": 0.2})
check("FINISHED" in bpy.ops.wm.rigforge_cleanup(mode="NORMALIZE"))
rows = rows_of(mesh)
check(
    all(
        abs(r.get("Bone", 0.0) - 0.5) < 1e-6
        and abs(r.get("Bone.001", 0.0) - 0.5) < 1e-6
        for r in rows
    ),
    "normalize did not scale",
)
paint(mesh, lambda v: {"Bone.001": 1.0} if v.index == 0 else {"Bone": 1.0})
check("FINISHED" in bpy.ops.wm.rigforge_cleanup(mode="SMOOTH", passes=5))
check(rows_of(mesh)[0].get("Bone.001", 0.0) < 1.0, "operator smooth no-op")
paint(mesh, lambda v: {"Arm.L": 1.0} if v.co.x > 0 else {})
check("FINISHED" in bpy.ops.wm.rigforge_cleanup(mode="MIRROR", subset="ALL"))
rows = rows_of(mesh)
# Off-center only: the center strip mirrors in place (module-tested).
check(
    all(rows[i] == {"Arm.R": 1.0} for i, x in enumerate(xs) if x < -1e-6),
    "mirror did not fill the right side",
)
check(
    all(rows[i] == {} for i, x in enumerate(xs) if x > 1e-6),
    "mirror kept stale left weights",
)
print("operator: prune/normalize/smooth/mirror OK")

# --- Default subset: deform-bone groups only, other groups untouched ---
check(
    nudge_mod.deform_group_names(mesh) == {"Bone", "Bone.001"},
    f"deform groups: {nudge_mod.deform_group_names(mesh)}",
)


def paint_masked(mesh):
    # Mask first (order must survive), locked, plus an empty pin group.
    mesh.vertex_groups.clear()
    mask = mesh.vertex_groups.new(name="Mask")
    pin = mesh.vertex_groups.new(name="ClothPin")
    bone = mesh.vertex_groups.new(name="Bone")
    child = mesh.vertex_groups.new(name="Bone.001")
    for v in mesh.data.vertices:
        mask.add([v.index], 0.25 + 0.5 * (v.index % 2), "REPLACE")
        bone.add([v.index], 0.3 if v.index == 0 else 0.6, "REPLACE")
        child.add([v.index], 0.004 if v.index == 0 else 0.2, "REPLACE")
    mask.lock_weight = True
    del pin


def mask_of(mesh):
    group = mesh.vertex_groups["Mask"]
    return [group.weight(v.index) for v in mesh.data.vertices]


paint_masked(mesh)
mask_before = mask_of(mesh)
for mode in ("LIMIT", "NORMALIZE", "PRUNE", "MIRROR", "SMOOTH"):
    paint_masked(mesh)
    result = bpy.ops.wm.rigforge_cleanup(mode=mode, limit=1, threshold=0.01)
    check("FINISHED" in result, f"{mode} did not finish: {result}")
    names = [g.name for g in mesh.vertex_groups]
    check(names[:2] == ["Mask", "ClothPin"], f"{mode} reordered groups: {names}")
    check(mesh.vertex_groups["Mask"].lock_weight, f"{mode} dropped the Mask lock")
    check(mask_of(mesh) == mask_before, f"{mode} touched the Mask weights")
    if mode != "MIRROR":  # mirror copies rows as-is (no renormalize)
        rows = nudge_mod.read_weights(mesh, {"Bone", "Bone.001"})[0]
        check(
            all(abs(sum(r.values()) - 1.0) < 1e-6 for r in rows if r),
            f"{mode} left bone rows unnormalized (Mask counted?)",
        )
paint_masked(mesh)
bpy.ops.wm.rigforge_cleanup(mode="LIMIT", limit=1)
rows = rows_of(mesh)
check(
    all(set(r) == {"Mask", "Bone"} for r in rows),
    "limit 1 should keep Bone + the untouched Mask",
)
paint_masked(mesh)
bpy.ops.wm.rigforge_cleanup(mode="PRUNE", threshold=0.01)
check("Bone.001" not in rows_of(mesh)[0], "prune missed the 0.004 speck")
check(rows_of(mesh)[1]["Bone.001"] > 0.2, "prune did not renormalize bones")
paint_masked(mesh)
bpy.ops.wm.rigforge_cleanup(mode="LIMIT", limit=1, subset="ALL")
check(
    all(len(r) == 1 for r in rows_of(mesh)),
    "All Groups should limit the Mask together with the bones",
)
print("operator: default subset keeps non-bone groups (order, lock, weights)")

paint(mesh, lambda v: {"Mask": 1.0})
try:
    result = bpy.ops.wm.rigforge_cleanup(mode="NORMALIZE")
except RuntimeError as exc:
    result = str(exc)
check("no deform-bone weights" in str(result), f"bone-less mesh gave {result}")
mod = next(m for m in mesh.modifiers if m.type == "ARMATURE")
mesh.modifiers.remove(mod)
check(nudge_mod.deform_group_names(mesh) is None, "unbound mesh has a rig?")
paint(mesh, lambda v: {f"B{b}": (b + 1) / 6.0 for b in range(6)})
check("FINISHED" in bpy.ops.wm.rigforge_cleanup(mode="LIMIT", limit=4))
check(all(len(r) == 4 for r in rows_of(mesh)), "unbound mesh should clean all")
print("operator: no-bone-weights error + unbound mesh cleans every group")


def expect_cancelled(fn, needle):
    try:
        result = fn()
    except RuntimeError as exc:
        check(needle in str(exc), f"wrong error: {exc}")
        return
    check("CANCELLED" in result, f"expected cancel, got {result}")


bare_data = bpy.data.meshes.new("Bare")
bare = bpy.data.objects.new("Bare", bare_data)
bpy.context.scene.collection.objects.link(bare)
bpy.ops.object.select_all(action="DESELECT")
bare.select_set(True)
bpy.context.view_layer.objects.active = bare
expect_cancelled(lambda: bpy.ops.wm.rigforge_cleanup(mode="LIMIT"), "no weights")
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
check(
    bpy.ops.wm.rigforge_cleanup.poll() is False,
    "operator should not poll on an armature",
)
check(
    "UNDO" in cleanup_op.WM_OT_rigforge_cleanup.bl_options,
    "operator lost its UNDO flag",
)
print("operator: error paths + undo-flag OK")

print("RIGFORGE_CLEANUP_OK")
