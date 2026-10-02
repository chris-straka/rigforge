# SPDX-License-Identifier: GPL-2.0-or-later
"""W1 gate: voxel weights beat Blender heat on the deformation test.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_weights_w1.py

Deformation test per ~/SWE/retopoforge/docs/deformation-test.md: three
procedural fixtures (tube limb, synthetic biped + fitted hll_hero,
coarsened synthetic quadruped + fitted hll_stalker), each skinned twice
(Blender automatic weights vs rigforge.weights.voxel) unless robust,
posed from tests/fixtures/weights_poses/*.json, measured per joint
region (verts with either joint-bone weight > 0.1): p95 stretch +
%>2x, area
distortion % (mean |log area ratio|, rigid-invariant candy-wrap
proxy; closed-slice volumes are ill-defined under weight-threshold
boundaries), flips. Gate: on the tube fixture at bends of 90 deg or
less, voxel p95 < heat p95 AND voxel area_dist < heat area_dist AND
voxel flips <= heat flips (strict unless both are 0) on every
(joint, pose) cell. Bends past 90 deg (elbow_135, knee_120) are
report-only: past 90 deg every LBS binder folds the inner crease
(flip-free needs weight gradient under cot(bend/2)/2r — at 135 deg
a 24 cm blend on the 50 cm tube), so the two crushed meshes compare
as noise; the run still records them. Hero/quad fixtures are
tracked report-only (W1b kernel R&D: the span kernel needs soft
per-bone distance decay plus adaptive blend width to match heat on
160-bone characters; see docs/weight-assist-plan.md). Robustness
fixtures (messy = production soup, micro = mm scale, far = bones
20 cm off the mesh) gate totality without a heat baseline (heat
exceeds usable budgets on degenerate soup): voxel must bind with
<5% fallbacks, re-bind bit-identically, and pose elbow_90 inside
smoke bounds (p95 < 2.5, area_dist < 25%, flips < 15% of region).

Jaw is skipped: the synthetic ball head has no mouth, so a jaw pose
would measure nothing anatomical (documented fixture limit, not a
method gap). Quad fixture is a coarser remesh of the synthetic
blockout (same shape, ~30k verts for gate speed) with no spine
joint: the Rigify horse mid-spine has no accessible FK flex, so a
spine cell would measure a rigid rotation (see quad.json note).

Pass criteria: exits 0 and prints RIGFORGE_WEIGHTS_W1_OK.

Env: RF_W1_ONLY=tube|hero|quad|messy|micro|far runs one fixture;
RF_W1_SMOOTH=N relaxes the hero/quad blockout surfaces N smooth
iterations before binding (lump-artifact probe: union blobs punish
volumetric methods).
"""

import json
import math
import os
import sys

import bpy
import numpy as np

POSES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "weights_poses"
)
REGION_W = 0.1  # joint region: either joint-bone weight above this
AXIS = {"X": 0, "Y": 1, "Z": 2}
REPORT_PATH = "/tmp/rigforge_w1_report.json"


def fail(msg):
    print("RIGFORGE_WEIGHTS_W1_FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def set_fk_mode(rig):
    """Limbs to FK so FK-control poses take effect (IK_FK 1.0 = FK end)."""
    for pb in rig.pose.bones:
        for key in list(pb.keys()):
            low = key.lower()
            if "ik" in low and "fk" in low and isinstance(pb[key], float):
                pb[key] = 1.0


def apply_pose(rig, pose):
    """Set control rotations from a pose dict; returns touched bones."""
    touched = []
    for bone, (axis, deg) in pose["set"].items():
        if bone not in rig.pose.bones:
            fail(f"pose bone {bone!r} missing from {rig.name}")
        pb = rig.pose.bones[bone]
        pb.rotation_mode = "XYZ"
        euler = [0.0, 0.0, 0.0]
        euler[AXIS[axis]] = math.radians(deg)
        pb.rotation_euler = euler
        touched.append(bone)
    bpy.context.view_layer.update()
    return touched


def bone_dir_posed(rig, bone):
    return np.array(tuple(rig.pose.bones[bone].matrix.col[1].to_3d()))


def bone_dir_rest(rig, bone):
    return np.array(tuple(rig.data.bones[bone].matrix_local.col[1].to_3d()))


def verify_pose(rig, tag, pose_name, pose):
    """Poses carry their own checks: bend angle and/or tail motion."""
    tol = math.radians(pose.get("tol", 25.0))
    if "bend" in pose:
        bone, want = pose["bend"]["bone"], math.radians(pose["bend"]["deg"])
        got = bone_dir_rest(rig, bone) @ bone_dir_posed(rig, bone)
        angle = math.acos(float(np.clip(got, -1.0, 1.0)))
        if abs(angle - want) > tol:
            fail(
                f"{tag}/{pose_name}: {bone} bent {math.degrees(angle):.1f}deg, "
                f"wanted {pose['bend']['deg']} (wrong axis/sign?)"
            )
    if "move" in pose:
        bone = pose["move"]["bone"]
        rest = np.array(tuple(rig.data.bones[bone].tail_local))
        mat = rig.pose.bones[bone].matrix
        head = np.array(tuple(mat.col[3][:3]))
        tail = head + np.array(tuple(mat.col[1].to_3d())) * rig.data.bones[bone].length
        disp = tail - rest
        want = np.array(pose["move"]["dir"], dtype=float)
        want /= np.linalg.norm(want)
        if np.linalg.norm(disp) < 0.002:
            fail(f"{tag}/{pose_name}: {bone} tail did not move (pose broken?)")
        if disp @ want / np.linalg.norm(disp) < 0.7:
            fail(
                f"{tag}/{pose_name}: {bone} tail moved {disp} "
                f"(wanted ~{pose['move']['dir']})"
            )


def clear_bones(rig, bones):
    for bone in bones:
        pb = rig.pose.bones[bone]
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.location = (0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()


def evaluated_positions(mesh):
    deps = bpy.context.evaluated_depsgraph_get()
    data = mesh.evaluated_get(deps).data
    return np.array([tuple(v.co) for v in data.vertices], dtype=np.float64)


def mesh_tris(mesh):
    mesh.data.calc_loop_triangles()
    return np.array(
        [tuple(t.vertices) for t in mesh.data.loop_triangles], dtype=np.int64
    )


def joint_region(mesh, bones):
    """Verts with either joint-bone weight above REGION_W."""
    groups = mesh.vertex_groups
    want = set()
    for bone in bones:
        idx = groups.find(bone)
        if idx < 0:
            fail(f"{mesh.name}: joint bone {bone!r} has no vertex group")
        want.add(idx)
    region = np.zeros(len(mesh.data.vertices), dtype=bool)
    for v in mesh.data.vertices:
        for el in v.groups:
            if el.group in want and el.weight > REGION_W:
                region[v.index] = True
                break
    return region


AREA_TOL = 1e-9  # |cross| = 2*area; legit min tri ~1e-5 (4 orders up)


def _tri_stretch(rest, posed, tris):
    """Max singular value of the deformation gradient per tri (nan: skip)."""
    out = np.full(len(tris), np.nan)
    if len(tris) == 0:
        return out, 0
    a1 = rest[tris[:, 1]] - rest[tris[:, 0]]
    a2 = rest[tris[:, 2]] - rest[tris[:, 0]]
    b1 = posed[tris[:, 1]] - posed[tris[:, 0]]
    b2 = posed[tris[:, 2]] - posed[tris[:, 0]]
    # Area guard (both states): needle tris from ngon subdivision have
    # long edges but ~zero area; their stretch is garbage and their
    # normal sign is a coin flip. Real crumple keeps finite area.
    ok = (np.linalg.norm(np.cross(a1, a2), axis=1) > AREA_TOL) & (
        np.linalg.norm(np.cross(b1, b2), axis=1) > AREA_TOL
    )
    n1 = np.linalg.norm(a1, axis=1)
    ok &= n1 > 1e-12
    u = np.zeros_like(a1)
    u[ok] = a1[ok] / n1[ok, None]
    proj = (a2 * u).sum(axis=1)
    w = a2 - proj[:, None] * u
    nw = np.linalg.norm(w, axis=1)
    ok &= nw > 1e-12
    v = np.zeros_like(a1)
    v[ok] = w[ok] / nw[ok, None]
    # F = posed2 @ inv(rest2) with rest2 upper-triangular.
    r00, r01, r11 = n1, proj, nw
    p00 = (b1 * u).sum(axis=1)
    p01 = (b2 * u).sum(axis=1)
    p10 = (b1 * v).sum(axis=1)
    p11 = (b2 * v).sum(axis=1)
    inv00 = np.zeros_like(r00)
    inv01 = np.zeros_like(r00)
    inv11 = np.zeros_like(r00)
    inv00[ok] = 1.0 / r00[ok]
    inv11[ok] = 1.0 / r11[ok]
    inv01[ok] = -r01[ok] / (r00[ok] * r11[ok])
    f00 = p00 * inv00
    f01 = p00 * inv01 + p01 * inv11
    f10 = p10 * inv00
    f11 = p10 * inv01 + p11 * inv11
    # smax = sqrt(max eig of F'F), closed form for 2x2 symmetric.
    m00 = f00 * f00 + f10 * f10
    m01 = f00 * f01 + f10 * f11
    m11 = f01 * f01 + f11 * f11
    trace = m00 + m11
    disc = np.clip((trace * 0.5) ** 2 - (m00 * m11 - m01 * m01), 0.0, None)
    out[ok] = np.sqrt(trace[ok] * 0.5 + np.sqrt(disc[ok]))
    return out, int(np.count_nonzero(~ok))


def _tri_flips(rest, posed, tris, ok_mask):
    a1 = rest[tris[:, 1]] - rest[tris[:, 0]]
    a2 = rest[tris[:, 2]] - rest[tris[:, 0]]
    b1 = posed[tris[:, 1]] - posed[tris[:, 0]]
    b2 = posed[tris[:, 2]] - posed[tris[:, 0]]
    dots = (np.cross(a1, a2) * np.cross(b1, b2)).sum(axis=1)
    return int(np.count_nonzero((dots < 0.0) & ok_mask))


def _patch_area_dist(rest, posed, tris, ok_mask):
    """Mean area-weighted |log area ratio| x100 over valid region tris.

    Candy-wrap proxy without caps: closed-slice volumes are ill-defined
    here (the region boundary is a weight-threshold contour inside the
    blend, so any cap swings with the pose and reports +-100% on rigid
    motion). Absolute log ratio so bulge gain cannot cancel pinch loss;
    rigid motion scores exactly 0. Crumple (area-preserving collapse)
    is caught by the flips metric instead.
    """
    tri = tris[ok_mask]
    a = rest[tri[:, 1]] - rest[tri[:, 0]]
    b = rest[tri[:, 2]] - rest[tri[:, 0]]
    c = posed[tri[:, 1]] - posed[tri[:, 0]]
    d = posed[tri[:, 2]] - posed[tri[:, 0]]
    ar = 0.5 * np.linalg.norm(np.cross(a, b), axis=1)
    ap = 0.5 * np.linalg.norm(np.cross(c, d), axis=1)
    lr = np.abs(np.log(ap / ar))
    w = ar / ar.sum()
    return float((lr * w).sum() * 100.0)


def measure(label, rest, posed, tris, region):
    """Stretch / volume / flips over a joint region; fails loud if empty."""
    tmask = region[tris].all(axis=1)
    tri = tris[tmask]
    if int(np.count_nonzero(region)) < 50:
        fail(f"{label}: joint region has <50 verts (joint not found?)")
    if len(tri) == 0:
        fail(f"{label}: joint region has no interior tris")
    smax, skipped = _tri_stretch(rest, posed, tri)
    valid = smax[np.isfinite(smax)]
    if len(valid) == 0:
        fail(f"{label}: all region tris degenerate")
    ok_mask = np.isfinite(smax)
    flips = _tri_flips(rest, posed, tri, ok_mask)
    area_dist = _patch_area_dist(rest, posed, tri, ok_mask)
    return {
        "p95": float(np.percentile(valid, 95)),
        "frac_gt2": float(np.mean(valid > 2.0)),
        "flips": flips,
        "area_dist_pct": area_dist,
        "region_verts": int(np.count_nonzero(region)),
        "region_tris": len(tri),
        "skipped_degen": skipped,
    }


bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402
from rigforge.auto_place.detect_landmarks import (  # noqa: E402
    build_synthetic,
    build_synthetic_quadruped,
)
from rigforge.weights import voxel  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__


def wipe():
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def duplicate_mesh(mesh, name):
    dup = mesh.copy()
    dup.data = mesh.data.copy()
    dup.name = name
    bpy.context.scene.collection.objects.link(dup)
    return dup


def heat_bind(mesh, rig):
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    return voxel.fill_unassigned(mesh)


def voxel_bind(mesh, rig):
    assignment, report = voxel.bind_weights(mesh, rig)
    voxel.apply_weights(mesh, assignment)
    voxel.ensure_armature_modifier(mesh, rig)
    filled = voxel.fill_unassigned(mesh)
    return report, filled


def build_tube():
    import bmesh

    wipe()
    # Dense along the limb (blend spans several rows), coarse around.
    # Odd height count so no vertex row sits exactly on the joint plane
    # (z=0.25): an exact-on-plane row takes 50/50 weights in every LBS
    # binder and folds its neighbor tris at 90 degrees. Real meshes
    # never place rows exactly on the plane (measure zero), so the
    # fixture straddles it like they do.
    mesh_data = bpy.data.meshes.new("Tube")
    bm = bmesh.new()
    n_around, n_height = 24, 41
    radius, z0, z1 = 0.05, 0.0, 0.5
    rings = []
    for j in range(n_height + 1):
        z = z0 + (z1 - z0) * j / n_height
        rings.append(
            [
                bm.verts.new(
                    (
                        radius * math.cos(2 * math.pi * i / n_around),
                        radius * math.sin(2 * math.pi * i / n_around),
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
    bm.faces.new(tuple(reversed(rings[0])))
    bm.faces.new(tuple(rings[-1]))
    bm.to_mesh(mesh_data)
    bm.free()
    mesh = bpy.data.objects.new("Tube", mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.armature_add(enter_editmode=True, location=(0, 0, 0))
    rig = bpy.context.view_layer.objects.active
    bones = rig.data.edit_bones
    bones[0].name = "Bone"
    bones[0].head = (0, 0, 0)
    bones[0].tail = (0, 0, 0.25)
    child = bones.new("Bone.001")
    child.head = (0, 0, 0.25)
    child.tail = (0, 0, 0.5)
    child.parent = bones[0]
    child.use_connect = True
    bpy.ops.object.mode_set(mode="OBJECT")
    return mesh, rig


def _maybe_smooth(mesh):
    iters = int(os.environ.get("RF_W1_SMOOTH", "0"))
    if iters > 0:
        bpy.context.view_layer.objects.active = mesh
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        mod = mesh.modifiers.new("rf_smooth", "SMOOTH")
        mod.factor = 0.5
        mod.iterations = iters
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.context.view_layer.objects.active = mesh


def build_hero():
    mesh, _spec = build_synthetic()
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.rigforge_hll_hero_metarig_add()
    meta = bpy.context.view_layer.objects.active
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta
    bpy.ops.wm.rigforge_auto_place()
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta
    bpy.ops.pose.rigforge_generate()
    rig = next(
        o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data
    )
    return mesh, rig


def build_quad():
    mesh, _spec = build_synthetic_quadruped()
    # Coarser remesh of the same shape for gate speed (201k -> ~30k).
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = mesh
    mod = mesh.modifiers.new("rx_coarse", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = 0.015
    mod.adaptivity = 0.0
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.object.rigforge_hll_stalker_metarig_add()
    meta = bpy.context.view_layer.objects.active
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta
    bpy.ops.wm.rigforge_auto_place()
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta
    bpy.ops.pose.rigforge_generate()
    rig = next(
        o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data
    )
    return mesh, rig


ROBUST_TAGS = ("messy", "micro", "far")  # totality gate, no heat baseline.
TRACKED_TAGS = ("hero", "quad")  # W1b kernel R&D; recorded, not gated.


def _soup_combo(mesh):
    """Production-soup mesh: needles + slits + doubled shell + chunk.

    Deterministic (seeded jitter). Heat exceeds usable budgets here
    (20+ min, killed); voxel must bind in seconds (totality gate).
    """
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh.data)
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=2)
    rng = np.random.default_rng(7)
    for v in bm.verts:
        v.co = tuple(np.array(tuple(v.co)) + rng.normal(0, 0.004, 3))
    bm.to_mesh(mesh.data)
    bm.free()
    bm = bmesh.new()
    bm.from_mesh(mesh.data)
    bm.faces.ensure_lookup_table()
    for f in list(bm.faces)[::7]:
        if len(f.verts) != 4:
            continue
        v0, v1 = f.verts[0], f.verts[1]
        n0 = bm.verts.new(tuple(v0.co))
        n1 = bm.verts.new(tuple(v1.co))
        loop_verts = [n0 if v == v0 else n1 if v == v1 else v for v in f.verts]
        bm.faces.new(loop_verts)
        bm.faces.remove(f)
    for f in list(bm.faces):
        dup = [bm.verts.new(tuple(v.co)) for v in f.verts]
        bm.faces.new(dup)
    vs = [
        bm.verts.new((0.3 + x, 0.3 + y, 0.3 + z))
        for x, y, z in [(0, 0, 0), (0.05, 0, 0), (0, 0.05, 0), (0, 0, 0.05)]
    ]
    bm.faces.new((vs[0], vs[1], vs[2]))
    bm.faces.new((vs[0], vs[1], vs[3]))
    bm.faces.new((vs[0], vs[2], vs[3]))
    bm.faces.new((vs[1], vs[2], vs[3]))
    bm.to_mesh(mesh.data)
    bm.free()


def build_messy():
    mesh, rig = build_tube()
    _soup_combo(mesh)
    return mesh, rig


def build_micro():
    mesh, rig = build_tube()
    mesh.scale = (0.005, 0.005, 0.005)
    rig.scale = (0.005, 0.005, 0.005)
    bpy.context.view_layer.update()
    return mesh, rig


def build_far():
    mesh, rig = build_tube()
    rig.location = (0.2, 0.0, 0.0)
    bpy.context.view_layer.update()
    return mesh, rig


def load_spec(tag):
    with open(os.path.join(POSES, f"{tag}.json"), encoding="utf-8") as f:
        return json.load(f)


def run_fixture(tag, mesh, rig, compare=True):
    spec = load_spec("messy" if tag in ROBUST_TAGS else tag)
    if tag in ("hero", "quad"):
        _maybe_smooth(mesh)
    if compare:
        mesh_heat = duplicate_mesh(mesh, f"{tag}_heat")
    mesh_vox = duplicate_mesh(mesh, f"{tag}_vox")
    bpy.data.objects.remove(mesh, do_unlink=True)
    if compare:
        heat_filled = heat_bind(mesh_heat, rig)
    vox_report, vox_filled = voxel_bind(mesh_vox, rig)
    print(
        f"[{tag}] bind: "
        + (f"heat gap-filled {heat_filled}, " if compare else "")
        + f"voxel cell={vox_report['cell']:.4f} "
        f"interior={vox_report['interior_voxels']} "
        f"fallback={vox_report['fallback_verts']} gap-filled {vox_filled}"
    )
    if not compare:
        # Robustness totality: bounded fallbacks + bit-identical re-bind.
        # Far bones sit outside flesh, so every vert takes the rigid
        # Euclidean-nearest fallback (~100% is the correct behavior).
        frac = vox_report["fallback_verts"] / len(mesh_vox.data.vertices)
        if tag == "far":
            check(frac > 0.95, f"{tag}: fallback fraction {frac:.3f} (want ~1.0)")
        else:
            check(frac < 0.05, f"{tag}: fallback fraction {frac:.3f} >= 5%")
        first, _ = voxel.bind_weights(mesh_vox, rig)
        second, _ = voxel.bind_weights(mesh_vox, rig)
        check(first == second, f"{tag}: re-bind differs (not deterministic)")
    set_fk_mode(rig)
    clear_bones(rig, [b for pose in spec["poses"].values() for b in pose["set"]])
    if compare:
        rest_heat = evaluated_positions(mesh_heat)
    rest_vox = evaluated_positions(mesh_vox)
    check(
        np.array_equal(rest_vox, evaluated_positions(mesh_vox)),
        f"{tag}: rest evaluation not deterministic",
    )
    tris_vox = mesh_tris(mesh_vox)
    if compare:
        tris_heat = mesh_tris(mesh_heat)
    cells = {}
    for joint, cfg in spec["joints"].items():
        region_vox = joint_region(mesh_vox, cfg["bones"])
        if compare:
            region_heat = joint_region(mesh_heat, cfg["bones"])
        for pose_name in cfg["poses"]:
            pose = spec["poses"][pose_name]
            touched = apply_pose(rig, pose)
            verify_pose(rig, tag, pose_name, pose)
            posed_vox = evaluated_positions(mesh_vox)
            if compare:
                posed_heat = evaluated_positions(mesh_heat)
            mv = measure(
                f"{tag}/{joint}/{pose_name}/vox",
                rest_vox,
                posed_vox,
                tris_vox,
                region_vox,
            )
            if compare:
                mh = measure(
                    f"{tag}/{joint}/{pose_name}/heat",
                    rest_heat,
                    posed_heat,
                    tris_heat,
                    region_heat,
                )
            clear_bones(rig, touched)
            key = f"{joint}/{pose_name}"
            if compare:
                cells[key] = {"heat": mh, "vox": mv}
                print(
                    f"[{tag}] {key}: heat p95={mh['p95']:.4f} "
                    f"ad={mh['area_dist_pct']:.2f}% flips={mh['flips']} "
                    f"| vox p95={mv['p95']:.4f} "
                    f"ad={mv['area_dist_pct']:.2f}% flips={mv['flips']}"
                )
            else:
                cells[key] = {"vox": mv}
                print(
                    f"[{tag}] {key}: vox p95={mv['p95']:.4f} "
                    f"ad={mv['area_dist_pct']:.2f}% flips={mv['flips']} "
                    f"(robustness, no heat baseline)"
                )
    return cells


results = {}
only = os.environ.get("RF_W1_ONLY")
if only is None or only == "tube":
    results["tube"] = run_fixture("tube", *build_tube())
if only is None or only == "hero":
    results["hero"] = run_fixture("hero", *build_hero())
if only is None or only == "quad":
    results["quad"] = run_fixture("quad", *build_quad())
if only is None or only == "messy":
    results["messy"] = run_fixture("messy", *build_messy(), compare=False)
if only is None or only == "micro":
    results["micro"] = run_fixture("micro", *build_micro(), compare=False)
if only is None or only == "far":
    results["far"] = run_fixture("far", *build_far(), compare=False)

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, sort_keys=True)

for tag, cells in results.items():
    spec = load_spec("messy" if tag in ROBUST_TAGS else tag)
    for key, cell in cells.items():
        mv = cell["vox"]
        _, pose_name = key.split("/", 1)
        bend_deg = spec["poses"][pose_name].get("bend", {}).get("deg", 0.0)
        if tag in TRACKED_TAGS:
            print(f"[{tag}] {key}: report-only (tracked, W1b R&D)")
            continue
        if tag in ROBUST_TAGS:
            # Smoke-grade: catches explosions/hangs/empties, not quality
            # (quality is the tube's job). Margins are 2x+ off measured.
            check(
                mv["p95"] < 2.5,
                f"{tag}/{key}: voxel p95 {mv['p95']} >= 2.5 (explosion?)",
            )
            check(
                mv["area_dist_pct"] < 25.0,
                f"{tag}/{key}: voxel area_dist {mv['area_dist_pct']} >= 25%",
            )
            flip_frac = mv["flips"] / mv["region_tris"]
            check(
                flip_frac < 0.15,
                f"{tag}/{key}: voxel flips {mv['flips']} >= 15% of region",
            )
            continue
        mh = cell["heat"]
        if bend_deg > 90.0:
            print(f"[{tag}] {key}: report-only (bend {bend_deg:g} deg)")
            continue
        if mv["flips"] > mh["flips"] or (
            mv["flips"] == mh["flips"] and mh["flips"] > 0
        ):
            fail(
                f"{tag}/{key}: voxel flips {mv['flips']} "
                f"not better than heat {mh['flips']}"
            )
        if not mv["p95"] < mh["p95"]:
            fail(f"{tag}/{key}: voxel p95 {mv['p95']} !< heat {mh['p95']}")
        if not mv["area_dist_pct"] < mh["area_dist_pct"]:
            fail(
                f"{tag}/{key}: voxel area_dist {mv['area_dist_pct']} !< "
                f"heat {mh['area_dist_pct']}"
            )
print(f"W1 report: {REPORT_PATH}")
print("RIGFORGE_WEIGHTS_W1_OK")
