# SPDX-License-Identifier: GPL-2.0-or-later
"""Detect rigging landmarks on a character mesh via slice/blob analysis.

Headless (from the repo root; needs no addon, works on factory startup):
    /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/detect_landmarks.py -- synthetic
    ... -- mesh.glb [--slices 120] [--facing -Y]

Method: cut the mesh with horizontal planes, cluster each cross-section
into blobs, and read the blob-count signature top to bottom. A biped in
A/T-pose shows 1 (head) -> 3+ (torso+arms) -> 1 (torso) -> 2 (legs);
the transitions locate neck / armpit / crotch, and cross-section minima
along tracked limb blobs locate elbow / wrist / knee / ankle.

Output: JSON landmarks on stdout between RIGFORGE_LANDMARKS_BEGIN/OK markers.
With --check, asserts synthetic-mesh landmarks against the calibrated spec
and exits nonzero on failure.
"""

import json
import sys
from itertools import pairwise

import bpy


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, a):
        parent = self.parent
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def clean_scene():
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
        for x in list(coll):
            coll.remove(x)


def add_ball(co, radius):
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=12, ring_count=8, radius=radius, location=co
    )
    return bpy.context.view_layer.objects.active


def add_box(co, size, scale):
    bpy.ops.mesh.primitive_cube_add(size=size, location=co)
    obj = bpy.context.view_layer.objects.active
    obj.scale = scale
    bpy.ops.object.transform_apply(scale=True)
    return obj


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b, strict=True))


def limb_balls(points):
    """Overlapping balls along each segment so boolean union connects them.

    points: sequence of (co, radius) with anatomical narrowing at joints.
    """
    parts = []
    for seg, ((a, ra), (b, rb)) in enumerate(pairwise(points)):
        for t in (0.0, 0.5, 1.0):
            if seg and t == 0.0:
                continue  # shared joint ball already placed
            parts.append(add_ball(lerp(a, b, t), ra + (rb - ra) * t))
    return parts


# Facing -Y (Andras convention: toes toward -Y). Units: meters.
# Calibrated against actual detector output; values must stay anatomically
# sane (ordered neck > armpit > elbow > wrist > hand, crotch > knee > ankle)
# and the --check tolerance below guards regressions.
SYNTHETIC_SPEC = {
    "height": 1.16,
    "neck_z": 0.97,
    "armpit_z": 0.80,
    "elbow_z": 0.66,
    "wrist_z": 0.48,
    "hand_tip_z": 0.35,
    "wrist_x": 0.35,
    "crotch_z": 0.40,
    "knee_z": 0.30,
    "ankle_z": 0.08,
    "ankle_x": 0.09,
}
CHECK_TOL = 0.06


def build_synthetic():
    """Single connected A-pose biped mesh; returns (object, spec)."""
    clean_scene()
    torso = add_box((0, 0, 0.72), 1.0, (0.30, 0.20, 0.55))
    parts = [
        add_box((0, 0, 0.52), 1.0, (0.26, 0.19, 0.25)),  # hips
        add_ball((0, 0, 1.06), 0.10),  # head
        add_ball((0, 0, 0.97), 0.05),  # neck
    ]
    for s in (1.0, -1.0):
        parts += limb_balls(
            [
                ((0.17 * s, 0, 0.88), 0.060),
                ((0.24 * s, 0, 0.77), 0.062),
                ((0.26 * s, 0, 0.66), 0.038),
                ((0.295 * s, 0, 0.57), 0.055),
                ((0.33 * s, 0, 0.48), 0.036),
            ]
        )
        parts.append(add_box((0.35 * s, -0.01, 0.41), 1.0, (0.10, 0.16, 0.12)))
        parts += limb_balls(
            [
                ((0.09 * s, 0, 0.50), 0.075),
                ((0.09 * s, 0, 0.40), 0.070),
                ((0.09 * s, 0, 0.30), 0.044),
                ((0.09 * s, 0, 0.19), 0.060),
                ((0.09 * s, 0, 0.08), 0.038),
            ]
        )
        parts.append(add_box((0.09 * s, -0.06, 0.035), 1.0, (0.09, 0.22, 0.07)))
    bpy.ops.object.mode_set(mode="OBJECT")
    for part in parts:
        mod = torso.modifiers.new("union", "BOOLEAN")
        mod.operation = "UNION"
        mod.object = part
        bpy.context.view_layer.objects.active = torso
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(part, do_unlink=True)
    return cleanup_union(torso), dict(SYNTHETIC_SPEC)


def cleanup_union(obj):
    """Rebuild a clean exterior via voxel remesh.

    Sequential exact-solver unions leave doubled faces and interior
    debris that read as phantom blobs in cross-sections. Voxel remesh
    keeps only the closed exterior (voxel size well below the smallest
    limb diameter, so joint narrowings survive).
    """
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new("rx", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = 0.006
    mod.adaptivity = 0.0
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return obj


def import_mesh(path):
    clean_scene()
    lower = path.lower()
    if lower.endswith((".glb", ".gltf")):
        bpy.ops.import_scene.gltf(filepath=path, import_pack_images=True)
    elif lower.endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=path, use_image_search=False)
    elif lower.endswith(".obj"):
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit(f"unsupported mesh format: {path}")
    meshes = [o for o in bpy.context.selected_objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit(f"no meshes imported from {path}")
    bpy.ops.object.mode_set(mode="OBJECT")
    for o in meshes:
        bpy.context.view_layer.objects.active = o
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if len(meshes) == 1:
        return meshes[0]
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def envelope_copy(obj):
    """Single-shell exterior copy for slicing.

    Production meshes stack shells (body + clothing + hair cards), and
    each shell adds contours to every cross-section it crosses. A voxel
    remesh fuses them into one exterior envelope; the original stays
    untouched for fitting later.
    """
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.duplicate()
    dup = bpy.context.view_layer.objects.active
    dup.data = dup.data.copy()
    zs = [v.co.z for v in dup.data.vertices]
    height = max(zs) - min(zs)
    mod = dup.modifiers.new("rx", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = height / 150
    mod.adaptivity = 0.0
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return dup


def slice_signature_axis(obj, slices=120, axis="z"):
    """True cross-sections: intersect mesh faces with planes ⊥ to `axis`.

    Blobs are connected components of the intersection graph: crossings
    that share a face are unioned, so contours stay whole no matter how
    coarse the tessellation. Returns (bands low-first, lo, hi).
    Each band: {"pos", axis-named position key, "blobs"}; blob: {"n",
    "r", "c<u>", "c<v>", "<u>min/max", "<v>min/max"} over the two plane
    axes (u, v), with r = mean radius in the slice plane.
    """
    if axis not in ("x", "y", "z"):
        raise SystemExit(f"slice axis must be x/y/z, got {axis!r}")
    ax_idx = "xyz".index(axis)
    plane = [a for a in "xyz" if a != axis]
    u_idx, v_idx = ("xyz".index(a) for a in plane)
    u_name, v_name = plane
    mesh = obj.data
    coords = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]
    lo = min(c[ax_idx] for c in coords)
    hi = max(c[ax_idx] for c in coords)
    span = hi - lo
    planes = [lo + (s + 0.5) / slices * span for s in range(slices)]
    buckets = [[] for _ in range(slices)]
    for poly in mesh.polygons:
        vs = poly.vertices
        plo = min(coords[i][ax_idx] for i in vs)
        phi = max(coords[i][ax_idx] for i in vs)
        if plo == phi:
            continue
        s0 = max(0, min(slices - 1, int((plo - lo) / span * slices)))
        s1 = max(0, min(slices - 1, int((phi - lo) / span * slices)))
        for s in range(s0, s1 + 1):
            buckets[s].append(tuple(vs))
    signature = []
    for s, at in enumerate(planes):
        points = []
        index_of = {}
        face_groups = []
        for vs in buckets[s]:
            ring = []
            nv = len(vs)
            for k in range(nv):
                a, b = vs[k], vs[(k + 1) % nv]
                ca, cb = coords[a], coords[b]
                if (ca[ax_idx] - at) * (cb[ax_idx] - at) < 0:
                    key = (a, b) if a < b else (b, a)
                    if key not in index_of:
                        t = (at - ca[ax_idx]) / (cb[ax_idx] - ca[ax_idx])
                        index_of[key] = len(points)
                        points.append(
                            (
                                ca[u_idx] + (cb[u_idx] - ca[u_idx]) * t,
                                ca[v_idx] + (cb[v_idx] - ca[v_idx]) * t,
                            )
                        )
                    ring.append(index_of[key])
            if len(ring) >= 2:
                face_groups.append(ring)
        uf = UnionFind(len(points))
        for ring in face_groups:
            for i in ring[1:]:
                uf.union(ring[0], i)
        groups = {}
        for i in range(len(points)):
            groups.setdefault(uf.find(i), []).append(i)
        blobs = []
        for idxs in groups.values():
            if len(idxs) < 3:
                continue
            cu = sum(points[i][0] for i in idxs) / len(idxs)
            cv = sum(points[i][1] for i in idxs) / len(idxs)
            r = sum(
                ((points[i][0] - cu) ** 2 + (points[i][1] - cv) ** 2) ** 0.5
                for i in idxs
            ) / len(idxs)
            blobs.append(
                {
                    "n": len(idxs),
                    f"c{u_name}": cu,
                    f"c{v_name}": cv,
                    "r": r,
                    f"{u_name}min": min(points[i][0] for i in idxs),
                    f"{u_name}max": max(points[i][0] for i in idxs),
                    f"{v_name}min": min(points[i][1] for i in idxs),
                    f"{v_name}max": max(points[i][1] for i in idxs),
                }
            )
        blobs.sort(key=lambda d: -d["n"])
        signature.append({"pos": at, axis: at, "blobs": blobs})
    return signature, lo, hi


def slice_signature(obj, slices=120):
    """True cross-sections: intersect mesh faces with horizontal planes.

    Biped path (axis="z"); see slice_signature_axis for the general form.
    Returns (bands bottom-first, zmin, zmax).
    """
    return slice_signature_axis(obj, slices, "z")


def smooth(values):
    return [
        sum(values[max(0, i - 1) : i + 2]) / len(values[max(0, i - 1) : i + 2])
        for i in range(len(values))
    ]


def argmin(values):
    return min(range(len(values)), key=values.__getitem__)


def argmin_interior(values, lo, hi):
    """Argmin over [lo, hi), excluding the range endpoints.

    Profile ranges end at region transitions (caps, merges) whose tiny or
    inflated radii would otherwise win every minimum search; a joint dip
    must lie strictly inside its search range.
    """
    if hi - lo > 2:
        lo, hi = lo + 1, hi - 1
    return lo + argmin(values[lo:hi])


def outer_blob(blobs, side):
    cands = [b for b in blobs if (b["cx"] > 0) == (side > 0)]
    return max(cands, key=lambda b: abs(b["cx"])) if cands else None


def track_profile(top_down, bands, side):
    """Tracked limb profile: list of (radius, cx), smoothed; None if lost."""
    prof = []
    for i in bands:
        b = outer_blob(top_down[i]["blobs"], side)
        prof.append((b["r"], b["cx"]) if b else None)
    known = [p for p in prof if p is not None]
    if len(known) < 3:
        return None
    fill, last = [], known[0]
    for p in prof:
        last = p if p is not None else last
        fill.append(last)
    return list(
        zip(
            smooth([p[0] for p in fill]),
            smooth([p[1] for p in fill]),
            strict=True,
        )
    )


def radii(prof):
    return [p[0] for p in prof]


def longest_run(idxs, max_gap=2):
    """Longest run of near-consecutive band indices (ties: topmost first).

    Stray bands (an ear pair high up, a toe split at the floor) must not
    stretch a limb region across the whole body; only the main run counts.
    """
    runs, cur = [], []
    for i in sorted(idxs):
        if cur and i - cur[-1] > max_gap:
            runs.append(cur)
            cur = []
        cur.append(i)
    if cur:
        runs.append(cur)
    return max(runs, key=len) if runs else []


def detect_biped(signature, zmin, zmax, facing):
    top_down = list(reversed(signature))
    counts = [len(s["blobs"]) for s in top_down]
    arms = longest_run([i for i, s in enumerate(top_down) if len(s["blobs"]) >= 3])
    legs = longest_run(
        [
            i
            for i, s in enumerate(top_down)
            if len(s["blobs"]) == 2 and (not arms or i > max(arms))
        ]
    )
    if not arms or not legs:
        raise SystemExit(f"no biped signature in blob counts: {counts}")
    armpit_z = top_down[min(arms)]["z"]
    hand_tip_z = top_down[max(arms)]["z"]
    crotch_z = top_down[min(legs)]["z"]
    head = [i for i in range(min(arms)) if len(top_down[i]["blobs"]) == 1]
    # Neck hides in the lower half of the head bands; the upper half holds
    # the skull cap, whose tiny tip rings would win a naive global minimum.
    low_head = head[len(head) // 2 :] if head else []
    neck_z = (
        top_down[min(low_head, key=lambda i: top_down[i]["blobs"][0]["r"])]["z"]
        if low_head
        else None
    )

    # Joints resolve distal-first with proximal exclusion: each minimum is
    # searched outside the already-found joint below it, so a deeper wrist
    # can never be mistaken for the elbow (and likewise ankle vs knee).
    # Wrist/elbow track the clean torso+2-arm slices only: finger splits
    # below the palm would otherwise win every radius minimum.
    arm_bands = [i for i in arms if len(top_down[i]["blobs"]) == 3]
    if len(arm_bands) < 6:
        arm_bands = arms
    arm = track_profile(top_down, arm_bands, 1.0)
    if arm is None:
        raise SystemExit("untrackable arm blobs")
    ar = radii(arm)
    third = 2 * len(arm_bands) // 3
    wrist_band = argmin_interior(ar, third, len(arm_bands))
    wrist_z = top_down[arm_bands[wrist_band]]["z"]
    above = [
        k for k in range(wrist_band) if top_down[arm_bands[k]]["z"] > wrist_z + 0.08
    ]
    if len(above) < 3:
        raise SystemExit("elbow search range collapsed")
    elbow_band = argmin_interior(ar, above[0], above[-1] + 1)
    elbow_z = top_down[arm_bands[elbow_band]]["z"]
    wrist_x = abs(arm[wrist_band][1])

    leg = track_profile(top_down, legs, 1.0)
    if leg is None:
        raise SystemExit("untrackable leg blobs")
    lr = radii(leg)
    half = len(legs) // 2
    knee_band = argmin_interior(lr, 0, half)
    knee_z = top_down[legs[knee_band]]["z"]
    below = [
        k
        for k in range(knee_band + 1, len(legs))
        if top_down[legs[k]]["z"] < knee_z - 0.08
    ]
    if not below:
        raise SystemExit("ankle search range collapsed")
    ankle_band = argmin_interior(lr, below[0], below[-1] + 1)
    ankle_z = top_down[legs[ankle_band]]["z"]
    ankle_x = abs(leg[ankle_band][1])

    # Facing from the feet: toes extend further forward than the heel
    # extends back. Overrides the assumed facing when unambiguous.
    foot = top_down[legs[-1]]["blobs"]
    spans = [(b["ymax"] - b["cy"], b["cy"] - b["ymin"]) for b in foot]
    toe_span = max(s[0] for s in spans)
    heel_span = max(s[1] for s in spans)
    if abs(toe_span - heel_span) > 0.01:
        facing = "+Y" if toe_span > heel_span else "-Y"

    def dip(prof, band, lo, hi):
        return round(max(prof[lo:hi]) - prof[band], 4)

    return {
        "facing": facing,
        "height": round(zmax - zmin, 4),
        "zmin": round(zmin, 4),
        "zmax": round(zmax, 4),
        "neck_z": round(neck_z, 4) if neck_z is not None else None,
        "armpit_z": round(armpit_z, 4),
        "elbow_z": round(elbow_z, 4),
        "wrist_z": round(wrist_z, 4),
        "hand_tip_z": round(hand_tip_z, 4),
        "wrist_x": round(wrist_x, 4),
        "crotch_z": round(crotch_z, 4),
        "knee_z": round(knee_z, 4),
        "ankle_z": round(ankle_z, 4),
        "ankle_x": round(ankle_x, 4),
        "wrist_dip": dip(ar, wrist_band, third, len(arm_bands)),
        "elbow_dip": dip(ar, elbow_band, above[0], above[-1] + 1),
        "knee_dip": dip(lr, knee_band, 0, half),
        "ankle_dip": dip(lr, ankle_band, below[0], below[-1] + 1),
    }


QUAD_SYNTHETIC_SPEC = {
    # Calibrated against actual detector output; keep anatomically sane.
    "length": 2.30,
    "spine_rear": [0.0, -0.70, 1.00],
    "spine_front": [0.0, 0.70, 1.00],
    "neck_base": [0.0, 0.80, 1.30],
    "head": [0.0, 1.05, 1.55],
    "skull": [0.0, 1.20, 1.55],
    "leg_top_z": 0.74,
    "leg_mid_z": 0.37,
    "leg_foot_z": 0.02,
    "leg_x": 0.30,
    "leg_front_y": 0.55,
    "leg_rear_y": -0.55,
    "tail_base": [0.0, -0.71, 0.98],
    "tail_tip": [0.0, -1.01, 0.80],
}
QUAD_CHECK_TOL = 0.09


def add_cylinder(co, radius, depth):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=16, radius=radius, depth=depth, location=co
    )
    return bpy.context.view_layer.objects.active


def add_cone(co, r1, r2, depth):
    bpy.ops.mesh.primitive_cone_add(
        vertices=16, radius1=r1, radius2=r2, depth=depth, location=co
    )
    return bpy.context.view_layer.objects.active


def build_synthetic_quadruped():
    """Single connected quadruped blockout (stalker-class); returns (obj, spec).

    Z-up, head toward +Y: torso box, neck + head balls, four straight
    leg cylinders, a tapering tail cone. Same boolean-union + voxel
    cleanup as the biped builder so the envelope path matches.
    """
    clean_scene()
    torso = add_box((0, 0, 1.00), 1.0, (0.50, 1.40, 0.50))
    parts = [
        add_ball((0, 0.80, 1.30), 0.16),  # neck
        add_ball((0, 1.05, 1.55), 0.20),  # head
    ]
    for sx in (1.0, -1.0):
        for sy in (1.0, -1.0):
            parts.append(add_cylinder((0.30 * sx, 0.55 * sy, 0.50), 0.09, 1.00))
    tail = add_cone((0, -0.85, 0.90), 0.09, 0.02, 0.40)
    tail.rotation_euler = (2.2, 0.0, 0.0)  # tip down-back, base in the rump
    bpy.context.view_layer.objects.active = tail
    bpy.ops.object.transform_apply(rotation=True)
    parts.append(tail)
    bpy.ops.object.mode_set(mode="OBJECT")
    for part in parts:
        mod = torso.modifiers.new("union", "BOOLEAN")
        mod.operation = "UNION"
        mod.object = part
        bpy.context.view_layer.objects.active = torso
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(part, do_unlink=True)
    return cleanup_union(torso), dict(QUAD_SYNTHETIC_SPEC)


def central_blob(blobs, x_frac=0.15, x_span=1.0, min_n=12):
    """Widest near-center blob (torso/neck/head), or None.

    Legs sit off-center (|cx| ~ stance) and ornaments (spine spikes,
    eyes) read as small-n blobs; the body column is central and big.
    """
    cands = [b for b in blobs if abs(b["cx"]) <= x_frac * x_span and b["n"] >= min_n]
    if not cands:
        return None
    return max(cands, key=lambda b: b["xmax"] - b["xmin"])


def detect_quadruped(long_sig, trans_sig, bounds, facing_hint="+Y"):
    """Quadruped landmarks: longitudinal spine analysis + legs from below.

    long_sig: planes ⊥ the spine axis (Y); trans_sig: horizontal planes.
    bounds: (xmin, xmax, ymin, ymax, zmin, zmax) of the envelope.
    Returns the quadruped landmark dict (kind="quadruped"). Leg ids are
    F/B (front/back of the torso center) x L/R (+X is L).
    """
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    x_span, y_span, z_span = xmax - xmin, ymax - ymin, zmax - zmin
    if not (y_span > x_span and y_span > z_span * 0.9):
        raise SystemExit(
            f"no quadruped signature: spans x={x_span:.3f} y={y_span:.3f} "
            f"z={z_span:.3f} (spine axis must be the longest horizontal)"
        )
    # Head end: the end whose top runs higher (head+neck vs rump/tail).
    # Compare vert tops over the outer 12% of the length at each end.
    head_hi = facing_hint == "+Y"
    # Central-column width profile along Y (low y first).
    widths, centers = [], []
    for s in long_sig:
        c = central_blob(s["blobs"], x_span=x_span)
        widths.append(0.0 if c is None else (c["xmax"] - c["xmin"]))
        centers.append(c)
    wide = [w for w in widths if w > 0]
    if not wide:
        raise SystemExit("no central body column in longitudinal slices")
    body_w = sorted(wide)[len(wide) // 2]
    is_wide = [w >= 0.6 * body_w for w in widths]
    # Wide runs: torso (longest) + head (wide run past a narrow neck gap
    # on the head side). Narrow gaps = neck, tail, or slice noise.
    runs, cur = [], []
    for i, w in enumerate(is_wide):
        if w:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    runs = [r for r in runs if len(r) >= 3]
    if not runs:
        raise SystemExit("no torso run in longitudinal slices")
    torso = max(runs, key=len)
    torso_c = sum(long_sig[i]["pos"] for i in torso) / len(torso)
    # Head side from the tops: compare zmax of the outer slices.
    end = max(3, len(long_sig) // 12)
    top_lo = max(
        (b["zmax"] for i in range(end) for b in long_sig[i]["blobs"]), default=zmin
    )
    top_hi = max(
        (
            b["zmax"]
            for i in range(len(long_sig) - end, len(long_sig))
            for b in long_sig[i]["blobs"]
        ),
        default=zmin,
    )
    head_hi = top_hi >= top_lo
    facing = "+Y" if head_hi else "-Y"
    if head_hi:
        rear_i, front_i = min(torso), max(torso)
    else:
        rear_i, front_i = max(torso), min(torso)
    spine_rear_c = centers[rear_i]
    spine_front_c = centers[front_i]
    if spine_rear_c is None or spine_front_c is None:
        raise SystemExit("torso run ends lost the body column")
    spine_rear = [spine_rear_c["cx"], long_sig[rear_i]["pos"], spine_rear_c["cz"]]
    spine_front = [spine_front_c["cx"], long_sig[front_i]["pos"], spine_front_c["cz"]]

    # Head run: wide runs past the torso on the head side (neck gap
    # between); neck_base = central centroid mid-gap.
    if head_hi:
        beyond = [r for r in runs if min(r) > front_i]
        gap = [i for i in range(front_i + 1, min(beyond[0]) if beyond else front_i + 1)]
    else:
        beyond = [r for r in runs if max(r) < rear_i]
        gap = [i for i in range((max(beyond[0]) + 1) if beyond else rear_i, rear_i)]
    head_run = max(beyond, key=len) if beyond else []
    neck_base = None
    for i in sorted(gap, key=lambda k: abs(k - sum(gap) / len(gap)) if gap else 0):
        if centers[i] is not None:
            neck_base = [centers[i]["cx"], long_sig[i]["pos"], centers[i]["cz"]]
            break
    if neck_base is None:  # neck fused with the torso: take the torso end top
        neck_base = [spine_front[0], spine_front[1], spine_front[2] + 0.15 * z_span]
    if head_run:
        # Widest slice, not most crossings: neck+head union perimeters
        # peak off-equator while the ball's width peaks at its equator.
        hpick = max(
            head_run,
            key=lambda i: centers[i]["xmax"] - centers[i]["xmin"] if centers[i] else -1,
        )
        hc = centers[hpick]
        head = [hc["cx"], long_sig[hpick]["pos"], hc["cz"]]
        hend = max(head_run) if head_hi else min(head_run)
        he = centers[hend]
        skull = (
            [he["cx"], long_sig[hend]["pos"], he["cz"]]
            if he is not None
            else list(head)
        )
    else:
        raise SystemExit("no head run past the neck gap")

    # Legs from below: the lowest transverse band with 4+ blobs; each
    # leg tracks upward (nearest centroid) until it merges (radius jump
    # or nearest blob shared with another leg).
    bottom_up = trans_sig  # low z first already
    ground = None
    for s in bottom_up:
        if len(s["blobs"]) >= 4:
            ground = s
            break
    if ground is None:
        counts = [len(s["blobs"]) for s in bottom_up[: len(bottom_up) // 4]]
        raise SystemExit(f"no 4-leg band near the ground (counts: {counts})")
    seeds = sorted((b for b in ground["blobs"] if b["n"] >= 6), key=lambda b: -b["n"])[
        :4
    ]
    if len(seeds) < 4:
        raise SystemExit(f"only {len(seeds)} trackable leg blobs at the ground")
    # Order seeds deterministically: front pair then rear pair, L then R.
    seeds.sort(key=lambda b: (b["cy"] < torso_c, -b["cx"]))
    legs = {}
    for rank, seed in enumerate(seeds):
        brow = "F" if seed["cy"] >= torso_c else "B"
        side = "L" if seed["cx"] >= 0 else "R"
        leg_id = brow + side
        if leg_id in legs:  # lopsided pair: keep both, suffix the extra
            leg_id = f"{brow}{side}{rank}"
        col = [(seed["cx"], seed["cy"], ground["pos"], seed["r"])]
        px, py, pr = seed["cx"], seed["cy"], seed["r"]
        for s in bottom_up[bottom_up.index(ground) + 1 :]:
            if not s["blobs"]:
                break
            nxt = min(
                s["blobs"], key=lambda b: (b["cx"] - px) ** 2 + (b["cy"] - py) ** 2
            )
            if (
                (nxt["cx"] - px) ** 2 + (nxt["cy"] - py) ** 2
            ) ** 0.5 > 0.12 * x_span + 0.03:
                break
            if nxt["r"] > 2.2 * pr + 0.02:
                break  # merged into the body mass
            col.append((nxt["cx"], nxt["cy"], s["pos"], nxt["r"]))
            px, py = nxt["cx"], nxt["cy"]
        top = col[-1]
        mid = col[len(col) // 2]
        foot = [col[0][0], col[0][1], col[0][2]]
        # Toe: front edge of the ground blob toward the facing side.
        edge = seed["ymax"] if head_hi else seed["ymin"]
        toe = [seed["cx"], edge, col[0][2] + 0.02 * z_span]
        legs[leg_id] = {
            "top": [round(top[0], 4), round(top[1], 4), round(top[2], 4)],
            "mid": [round(mid[0], 4), round(mid[1], 4), round(mid[2], 4)],
            "foot": [round(v, 4) for v in foot],
            "toe": [round(toe[0], 4), round(toe[1], 4), round(toe[2], 4)],
            "bands": len(col),
        }

    # Tail: past the torso rear end, near or below the spine line
    # (dorsal spikes ride above it and must not read as tail); the
    # stalker blockout has no tail geometry, so this is often a loud
    # fallback. tail_base = nearest tail blob to the torso (the rump
    # exit); tail_tip = farthest.
    tail_base, tail_tip, tail_fallback = None, None, False
    tail_idx = (
        [i for i in range(0, rear_i)]
        if head_hi
        else [i for i in range(rear_i + 1, len(long_sig))]
    )
    tail_order = list(reversed(tail_idx)) if head_hi else list(tail_idx)
    for i in tail_order:
        for b in long_sig[i]["blobs"]:
            if b["cz"] < spine_rear[2] + 0.05 * z_span and abs(b["cx"]) < 0.3 * x_span:
                tail_base = [b["cx"], long_sig[i]["pos"], b["cz"]]
                break
        if tail_base is not None:
            break
    for i in reversed(tail_order):
        for b in long_sig[i]["blobs"]:
            if b["cz"] < spine_rear[2] + 0.05 * z_span and abs(b["cx"]) < 0.3 * x_span:
                tail_tip = [b["cx"], long_sig[i]["pos"], b["cz"]]
                break
        if tail_tip is not None:
            break
    if tail_base is None:
        tail_base = list(spine_rear)
        tail_fallback = True

    def r4(v):
        return [round(c, 4) for c in v]

    return {
        "kind": "quadruped",
        "facing": facing,
        "spine_axis": "Y",
        "length": round(ymax - ymin, 4),
        "height": round(zmax - zmin, 4),
        "bounds": {
            "xmin": round(xmin, 4),
            "xmax": round(xmax, 4),
            "ymin": round(ymin, 4),
            "ymax": round(ymax, 4),
            "zmin": round(zmin, 4),
            "zmax": round(zmax, 4),
        },
        "spine_front": r4(spine_front),
        "spine_rear": r4(spine_rear),
        "neck_base": r4(neck_base),
        "head": r4(head),
        "skull": r4(skull),
        "legs": legs,
        "tail_base": r4(tail_base),
        "tail_tip": r4(tail_tip) if tail_tip is not None else None,
        "tail_base_fallback": tail_fallback,
        "stance": {k: [v["foot"][0], v["foot"][1]] for k, v in legs.items()},
        "torso_y": round(torso_c, 4),
        "body_width": round(body_w, 4),
    }


def check_quad(spec, landmarks):
    """Compare quadruped landmarks against the build spec; returns bad dict."""
    bad = {}
    for k, v in spec.items():
        got = landmarks.get(k)
        if got is None and k.startswith("leg_"):
            vals = []
            for leg in landmarks.get("legs", {}).values():
                if k == "leg_top_z":
                    vals.append(leg["top"][2])
                elif k == "leg_mid_z":
                    vals.append(leg["mid"][2])
                elif k == "leg_foot_z":
                    vals.append(leg["foot"][2])
                elif k == "leg_x":
                    vals.append(abs(leg["foot"][0]))
                elif k == "leg_front_y":
                    if leg["foot"][1] > landmarks["torso_y"]:
                        vals.append(leg["foot"][1])
                elif k == "leg_rear_y":
                    if leg["foot"][1] < landmarks["torso_y"]:
                        vals.append(leg["foot"][1])
            got = sum(vals) / len(vals) if vals else None
        if got is None:
            bad[k] = (None, v)
        elif isinstance(v, list):
            d = sum((a - b) ** 2 for a, b in zip(got, v, strict=True)) ** 0.5
            if d > QUAD_CHECK_TOL:
                bad[k] = (got, v)
        elif abs(got - v) > QUAD_CHECK_TOL:
            bad[k] = (got, v)
    return bad


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    src = args[0] if args else "synthetic"
    slices = int(args[args.index("--slices") + 1]) if "--slices" in args else 120
    facing = args[args.index("--facing") + 1] if "--facing" in args else "-Y"
    kind = args[args.index("--kind") + 1] if "--kind" in args else None
    if kind is None:
        kind = "quadruped" if src == "synthetic-quad" else "biped"
    if kind not in ("biped", "quadruped"):
        raise SystemExit(f"--kind must be biped or quadruped, got {kind!r}")
    check = "--check" in args

    spec = None
    if src == "synthetic":
        target, spec = build_synthetic()
    elif src == "synthetic-quad":
        target, spec = build_synthetic_quadruped()
    else:
        target = envelope_copy(import_mesh(src))
    if kind == "quadruped":
        long_sig, ymin, ymax = slice_signature_axis(target, slices, "y")
        trans_sig, zmin, zmax = slice_signature_axis(target, slices, "z")
        mesh = target.data
        xs = [v.co.x for v in mesh.vertices]
        bounds = (min(xs), max(xs), ymin, ymax, zmin, zmax)
        if "--dump" in args:
            print("COUNTS-Y:", [len(s["blobs"]) for s in long_sig])
            print("COUNTS-Z:", [len(s["blobs"]) for s in trans_sig])
        landmarks = detect_quadruped(long_sig, trans_sig, bounds, facing)
    else:
        signature, zmin, zmax = slice_signature(target, slices)
        if "--dump" in args:
            print("COUNTS:", [len(s["blobs"]) for s in reversed(signature)])
        landmarks = detect_biped(signature, zmin, zmax, facing)
        landmarks["kind"] = "biped"
    print("RIGFORGE_LANDMARKS_BEGIN")
    print(json.dumps(landmarks, indent=2))
    if check:
        if spec is None:
            raise SystemExit("--check needs a synthetic mesh")
        if kind == "quadruped":
            bad = check_quad(spec, landmarks)
        else:
            bad = {
                k: (landmarks[k], v)
                for k, v in spec.items()
                if landmarks.get(k) is None or abs(landmarks[k] - v) > CHECK_TOL
            }
            ordered = ["neck_z", "armpit_z", "elbow_z", "wrist_z", "hand_tip_z"]
            ordered += ["crotch_z", "knee_z", "ankle_z"]
            for a, b in pairwise(ordered):
                if (
                    landmarks.get(a) is not None
                    and landmarks.get(b) is not None
                    and a.split("_")[0] != "hand"
                    and b.split("_")[0] != "crotch"
                    and not landmarks[a] > landmarks[b]
                ):
                    bad[f"order:{a}>{b}"] = (landmarks[a], landmarks[b])
        if bad:
            print("CHECK FAIL:", json.dumps(bad))
            raise SystemExit("landmark check failed")
        print("CHECK OK")
    print("RIGFORGE_LANDMARKS_OK")


if __name__ == "__main__":
    main()
