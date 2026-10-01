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


def slice_signature(obj, slices=120):
    """True cross-sections: intersect mesh faces with horizontal planes.

    Blobs are connected components of the intersection graph: crossings
    that share a face are unioned, so contours stay whole no matter how
    coarse the tessellation. Returns (bands bottom-first, zmin, zmax).
    Each band: {"z", "blobs"}; blob: {"n", "cx", "cy", "r"} with r = mean
    radius in the slice plane.
    """
    mesh = obj.data
    coords = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]
    zmin = min(c[2] for c in coords)
    zmax = max(c[2] for c in coords)
    height = zmax - zmin
    planes = [zmin + (s + 0.5) / slices * height for s in range(slices)]
    buckets = [[] for _ in range(slices)]
    for poly in mesh.polygons:
        vs = poly.vertices
        lo = min(coords[i][2] for i in vs)
        hi = max(coords[i][2] for i in vs)
        if lo == hi:
            continue
        s0 = max(0, min(slices - 1, int((lo - zmin) / height * slices)))
        s1 = max(0, min(slices - 1, int((hi - zmin) / height * slices)))
        for s in range(s0, s1 + 1):
            buckets[s].append(tuple(vs))
    signature = []
    for s, z in enumerate(planes):
        points = []
        index_of = {}
        face_groups = []
        for vs in buckets[s]:
            ring = []
            nv = len(vs)
            for k in range(nv):
                a, b = vs[k], vs[(k + 1) % nv]
                ax, ay, az = coords[a]
                bx, by, bz = coords[b]
                if (az - z) * (bz - z) < 0:
                    key = (a, b) if a < b else (b, a)
                    if key not in index_of:
                        t = (z - az) / (bz - az)
                        index_of[key] = len(points)
                        points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
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
            cx = sum(points[i][0] for i in idxs) / len(idxs)
            cy = sum(points[i][1] for i in idxs) / len(idxs)
            r = sum(
                ((points[i][0] - cx) ** 2 + (points[i][1] - cy) ** 2) ** 0.5
                for i in idxs
            ) / len(idxs)
            blobs.append(
                {
                    "n": len(idxs),
                    "cx": cx,
                    "cy": cy,
                    "r": r,
                    "ymin": min(points[i][1] for i in idxs),
                    "ymax": max(points[i][1] for i in idxs),
                }
            )
        blobs.sort(key=lambda d: -d["n"])
        signature.append({"z": z, "blobs": blobs})
    return signature, zmin, zmax


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


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    src = args[0] if args else "synthetic"
    slices = int(args[args.index("--slices") + 1]) if "--slices" in args else 120
    facing = args[args.index("--facing") + 1] if "--facing" in args else "-Y"
    check = "--check" in args

    spec = None
    if src == "synthetic":
        target, spec = build_synthetic()
    else:
        target = envelope_copy(import_mesh(src))
    signature, zmin, zmax = slice_signature(target, slices)
    if "--dump" in args:
        print("COUNTS:", [len(s["blobs"]) for s in reversed(signature)])
    landmarks = detect_biped(signature, zmin, zmax, facing)
    print("RIGFORGE_LANDMARKS_BEGIN")
    print(json.dumps(landmarks, indent=2))
    if check:
        if spec is None:
            raise SystemExit("--check needs the synthetic mesh")
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
