# SPDX-License-Identifier: GPL-2.0-or-later
"""Validation gate for a fitted metarig (auto-placement Phase 3).

Headless (from the repo root; the preset fallback needs the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/validate_fit.py -- \\
      MESH FITTED_METARIG.py LANDMARKS.json [--margin M] [--render DIR]
      [--no-render] [--on-fail stop|preset] [--open bone,...] [--report PATH]
      [--closeup PNG]

`--closeup PNG` skips the gate and renders a labeled-points toe-region
close-up (PNG + a sibling _labels.json with pixel coords for text
overlay); it exists to eyeball foot bones and catch strays.

A fitted metarig must pass this gate before it is allowed near `generate`
(docs/auto_placement.md Section 4.4). The driver set is structural:
metarigs with tail.001 take the stalker drivers (quadruped landmarks
required), others the hero drivers. Hard gates (failures stop the
pipeline, naming bones and what to fix):

- closure: every connected driver joint gap < 1e-4 (same epsilon as
  `reconnect_from`), or the open bone is explicitly listed via --open.
  (The hll_hero stock preset floats 7 driver joints by design — socket,
  hip, and neck pivots with real gaps — so a fitted hero needs
  --open shoulder.L,shoulder.R,spine.004,thigh.L,thigh.R,upper_arm.L,upper_arm.R.)
- inside-mesh WITH MARGIN: each driver-bone midpoint must raycast inside
  the fused-exterior envelope (parity over 6 axis rays) AND sit at least
  --margin (default 1 cm) from the surface. Mixamo tails showed that
  merely-inside still breaches the silhouette visually.
- symmetry: worst L/R bone-mirror error < 1 cm.

Warn-only placement reports (never fail the gate):

- fingers: per-finger chain direction vs the mesh finger direction
  (centroid split of hand-slab verts: below the wrist but above the
  slice fingertip floor, so boot soles at z~0 never enter); warns past
  45 deg. PLUS a positional check the angle misses: fingertip
  containment — any fingertip outside the envelope warns, with
  per-finger lateral-vs-axis vectors naming the nudge (reported, never
  gated: healthy and bad fits share the same medial bias magnitude).
  Thumb included in the angle check: it measures ~26 deg on Andras,
  i.e. it tracks the fan in this metarig — the old "points sideways"
  rationale did not match the rig (its tip distance stays reported
  separately, ungated: the thumb grazes the surface by anatomy).
- toes: toe-tip overshoot past the mesh foot front in the facing axis,
  PLUS toe-axis yaw vs the mesh foot direction (front/back-half split
  of sub-ankle verts; warns past 45 deg, same pattern as fingers).
  Tip shortfall is reported, never gated (healthy and bad fits share
  it: rigid preset toe, no toe landmark yet). (The sideways red stubs
  at the feet are the heel.02 reverse-foot pivots, correct by
  design — not toes.)
- head: head-top vs a skullcap estimate (widest head-blob ring + its
  radius), NOT mesh zmax — hair spikes must not count.
- face: face-bone centroid must sit inside the head blob between a chin
  floor (lowest single-blob band above the neck) and the skullcap, and
  the face bottom must clear the chin floor (gross-misplacement
  tripwires only). Brow clearance, face frac, and per-part geometry are
  reported, never gated — calibration forbids a threshold (healthy
  synthetic reads -0.3 cm brow clearance vs Andras's +2.1 cm: hair
  inflates the cap reference). Heuristic only: slices cannot see mesh
  features (eyes/nose/mouth) and cannot separate hair from skull.

The fit report carries per-driver-bone surface-distance stats (head / mid
/ tail depths, min, mean) plus overlay render paths (front/side PNGs and
a joints JSON with camera-projected driver joints for overlay).

Fallback ladder (never silently ship a bad fit): a fitted pass is rung 0
(full_auto). A fitted fail stops with exit 1 and a bones-and-fixes report
(rung 1, artist confirm) unless --on-fail preset, which additionally
validates the stock preset as-is, attaches its report, and exits 0 with
verdict preset_as_is (rung 2). Markers: RIGFORGE_VALIDATE_OK (full_auto),
RIGFORGE_VALIDATE_PRESET (rung 2), RIGFORGE_VALIDATE_FAIL (stopped).

`run_gate()` is importable (same load_tool pattern as the fitter) so
tests can validate an in-session mesh + metarig without a subprocess.
"""

import importlib.util
import json
import math
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

CLOSE_EPS = 1e-4  # same epsilon as reconnect_from
SYMMETRY_TOL = 0.01  # Section 4.4: L/R mirror error must be < 1 cm
MARGIN_DEFAULT = 0.01  # inside-mesh margin: silhouette rule, not just inside
FINGER_WARN_DEG = 45.0  # finger chain vs mesh finger direction
# Kept at 45: the preset's natural fan reads 38 deg on the outer fingers
# (clean synthetic mesh), so the threshold stays above accepted anatomy
# with headroom. (The old "pinky 32 deg fine" Andras reading is obsolete:
# it was measured against a sole-contaminated reference; the corrected
# reference reads 46 deg on that same pinky, consistent with its 4.3 cm
# offset and outside-mesh parity.)
# No lateral threshold: the healthy synthetic fit reads 4.9 cm of fan-vs-
# axis lateral (fat hand box, same medial bias as Andras's 4.3 cm), so no
# lateral line separates healthy from bad. The gate warns on tip
# containment (binary, anatomical); lateral vectors stay as diagnostics.
TOE_WARN = 0.002  # toe-tip overshoot past the mesh foot front
TOE_DIR_WARN = 45.0  # toe-axis yaw vs mesh foot direction
# No shortfall threshold: the healthy synthetic fit reads 6.7 cm short
# (crude 22 cm foot box, rigid preset toe) against Andras's 3.3 cm, so
# no shortfall line separates healthy from bad. Shortfall stays a
# reported diagnostic; the hard margin gate already fails a toe riding
# the surface (Andras toe.L 7.2 mm, eyeballed a real breach).
HEAD_WARN = 0.03  # head-top vs skullcap estimate
# No brow-clearance threshold: the healthy synthetic fit reads -0.3 cm
# (cap noise underestimates the ball top) against Andras's +2.1 cm, so
# the ordering inverts — hair inflates the skullcap reference in exactly
# the wrong direction. Brow clearance, face frac, and per-part geometry
# stay reported diagnostics; the gate trips only on gross misplacement
# (centroid outside the head blob, face sunk past the chin floor).
SLAB_BELOW_TIP = 0.02  # hand-slab floor below the slice fingertip floor
INSIDE_VOTES = 5  # parity rays (of 6) that must read inside
RAY_EPS = 1e-4
RAY_MAX_HITS = 8

# Section 4.3 hero drivers (fingers rigid-follow the hand; face follows head).
DRIVER_BONES = (
    "spine",
    "spine.001",
    "spine.002",
    "spine.003",
    "spine.004",
    "spine.005",
    "spine.006",
    "shoulder.L",
    "upper_arm.L",
    "forearm.L",
    "hand.L",
    "thigh.L",
    "shin.L",
    "foot.L",
    "toe.L",
    "shoulder.R",
    "upper_arm.R",
    "forearm.R",
    "hand.R",
    "thigh.R",
    "shin.R",
    "foot.R",
    "toe.R",
)

# Section 4.3 stalker drivers. Skull/ears/jaw/eyes/nose rigid-follow the
# head (the quadruped "face": warn-only, same as the hero face). The
# tail joins the hard set only when tail geometry was measured; with
# the fallback tail base there is no geometry to verify against, so the
# tail is warn-only ("unverified") instead of failing the mesh it never
# had. See drivers_for().
STALKER_MIDLINE = (
    "spine.001",
    "spine.002",
    "spine.003",
    "spine.004",
    "spine.005",
    "spine.006",
    "neck.001",
    "neck.002",
    "neck.003",
    "neck.004",
    "head",
)
STALKER_LEGS = (
    "shoulder.L",
    "upper_arm.L",
    "forearm.L",
    "forefoot.L",
    "pelvis.L",
    "thigh.L",
    "lower_leg.L",
    "hind_foot.L",
    "shoulder.R",
    "upper_arm.R",
    "forearm.R",
    "forefoot.R",
    "pelvis.R",
    "thigh.R",
    "lower_leg.R",
    "hind_foot.R",
)
# Toe/hoof chains are warn-only (quad_toes), never hard-gated: the toe
# landmark marks the foot blob's front edge (a surface feature, not a
# measured joint) and blockout feet contain no toe sub-structure to
# verify a joint against. Calibration: the stalker passes them at
# 15.7 mm while the thinner-legged synthetic (leg r 0.09 vs 0.098)
# flips them outside — the check tests flesh radius, not fit. Same
# unmeasurable -> warn-only principle as the hero face and the
# fallback tail. Placement stays visible via quad_toes containment.
STALKER_TOES = (
    "f_toe.L",
    "f_hoof.L",
    "r_toe.L",
    "r_hoof.L",
    "f_toe.R",
    "f_hoof.R",
    "r_toe.R",
    "r_hoof.R",
)
STALKER_TAIL = ("tail.001", "tail.002", "tail.003", "tail.004", "tail.005")


def drivers_for(meta, lm):
    """Hard-gate driver set: stalker metarigs get stalker drivers.

    Detection is structural (tail.001 exists only on the quadruped
    preset), cross-checked against the landmarks kind. Returns
    (drivers, tail_gated): the tail is hard-gated only with measured
    tail geometry (see STALKER_TAIL note above).
    """
    bones = meta.data.edit_bones if meta.mode == "EDIT" else meta.data.bones
    is_stalker = "tail.001" in bones
    kind = (lm or {}).get("kind", "biped")
    if is_stalker != (kind == "quadruped"):
        fail(
            f"metarig/landmarks mismatch: metarig "
            f"{'has' if is_stalker else 'lacks'} tail.001 but kind={kind!r}"
        )
    if not is_stalker:
        return DRIVER_BONES, False
    tail_gated = not (lm or {}).get("tail_base_fallback", False)
    drivers = STALKER_MIDLINE + STALKER_LEGS + (STALKER_TAIL if tail_gated else ())
    return drivers, tail_gated


FINGERS = ("f_index", "f_middle", "f_ring", "f_pinky")
SIDES = ("L", "R")

DIRS = (
    Vector((1.0, 0.0, 0.0)),
    Vector((-1.0, 0.0, 0.0)),
    Vector((0.0, 1.0, 0.0)),
    Vector((0.0, -1.0, 0.0)),
    Vector((0.0, 0.0, 1.0)),
    Vector((0.0, 0.0, -1.0)),
)


def fail(msg):
    print("RIGFORGE_VALIDATE_FAIL:", msg)
    raise SystemExit(msg)


def load_tool(name, filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_metarig_from_module(path):
    """Rebuild a metarig object from an emitted write_metarig module."""
    with open(path, encoding="utf-8") as f:
        code = f.read()
    ns = {}
    exec(compile(code, path, "exec"), ns)
    if "create" not in ns:
        fail(f"{path} has no create() (not a write_metarig module)")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.armature_add(enter_editmode=False, location=(0, 0, 0))
    obj = bpy.context.view_layer.objects.active
    # create() only adds bones, so clear the armature_add default first:
    # the leftover 1 m "Bone" at the origin rendered as a mystery tube
    # between the legs in every gate overlay until this line existed.
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    for eb in list(obj.data.edit_bones):
        obj.data.edit_bones.remove(eb)
    bpy.ops.object.mode_set(mode="OBJECT")
    ns["create"](obj)
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def check_closure(meta, drivers=DRIVER_BONES):
    """Connected-joint gaps + open drivers; runs in EDIT mode."""
    bpy.context.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode="EDIT")
    bones = meta.data.edit_bones
    max_gap, gaps, open_drivers = 0.0, {}, []
    for b in bones:
        if b.parent is None:
            continue
        gap = (b.head - b.parent.tail).length
        if b.use_connect:
            max_gap = max(max_gap, gap)
            if gap >= CLOSE_EPS:
                gaps[b.name] = gap
        elif b.name in drivers:
            open_drivers.append(b.name)
    return {
        "max_gap": max_gap,
        "gaps": gaps,
        "open_drivers": sorted(open_drivers),
    }


def ray_parity(eval_obj, origin):
    """March each axis ray through the mesh; odd crossings read inside."""
    out = []
    for d in DIRS:
        hits = []
        o = origin + d * RAY_EPS
        for _ in range(RAY_MAX_HITS):
            hit, loc, _, _ = eval_obj.ray_cast(o, d)
            if not hit:
                break
            hits.append((loc - origin).length)
            o = loc + d * RAY_EPS
        out.append((len(hits) % 2 == 1, hits[0] if hits else None))
    return out


def point_depth(eval_obj, origin):
    """Inside verdict (parity votes) + nearest-surface distance."""
    res = ray_parity(eval_obj, origin)
    votes = sum(1 for inside, _ in res if inside)
    firsts = [d for _, d in res if d is not None]
    return {
        "inside": votes >= INSIDE_VOTES,
        "votes": votes,
        "depth": min(firsts) if firsts else 0.0,
        "missed": sum(1 for _, d in res if d is None),
    }


def check_inside(meta, eval_env, margin, drivers=DRIVER_BONES):
    """Per-driver-bone surface stats; midpoint must be inside + margin."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    stats, failures = {}, []
    for name in drivers:
        b = bones.get(name)
        if b is None:
            failures.append({"bone": name, "reason": "missing driver bone"})
            continue
        mid = mat @ ((b.head + b.tail) / 2)
        ph = point_depth(eval_env, mat @ b.head)
        pm = point_depth(eval_env, mid)
        pt = point_depth(eval_env, mat @ b.tail)
        depths = (ph["depth"], pm["depth"], pt["depth"])
        ok = pm["inside"] and pm["depth"] >= margin
        stats[name] = {
            "inside": pm["inside"],
            "votes": pm["votes"],
            "head": round(ph["depth"], 4),
            "mid": round(pm["depth"], 4),
            "tail": round(pt["depth"], 4),
            "min": round(min(depths), 4),
            "mean": round(sum(depths) / 3, 4),
            "margin_ok": ok,
        }
        if not ok:
            failures.append(
                {
                    "bone": name,
                    "inside": pm["inside"],
                    "votes": pm["votes"],
                    "depth": round(pm["depth"], 4),
                }
            )
    return stats, failures


def check_symmetry(meta):
    """Worst L/R mirror error over paired bones (local build coords)."""
    bones = meta.data.edit_bones
    worst, worst_pair = 0.0, None
    for b in bones:
        if not b.name.endswith(".L"):
            continue
        other = bones.get(b.name[:-2] + ".R")
        if other is None:
            continue
        err = max(
            abs(b.head.x + other.head.x),
            abs(b.head.y - other.head.y),
            abs(b.head.z - other.head.z),
            abs(b.tail.x + other.tail.x),
            abs(b.tail.y - other.tail.y),
            abs(b.tail.z - other.tail.z),
        )
        if err > worst:
            worst, worst_pair = err, b.name
    return {"worst": worst, "pair": worst_pair}


def centroid(vs):
    n = len(vs)
    return Vector(
        (
            sum(v.x for v in vs) / n,
            sum(v.y for v in vs) / n,
            sum(v.z for v in vs) / n,
        )
    )


def finger_slab(env, lm, side):
    """Hand-slab verts: below the wrist but above the fingertip floor.

    The old unbounded sub-wrist set caught boot soles at z~0 (Andras:
    266 of 853 verts), dragging the finger reference toward the floor.
    """
    sx = lm["wrist_x"] if side == "L" else -lm["wrist_x"]
    floor = lm["hand_tip_z"] - SLAB_BELOW_TIP
    return [
        v.co
        for v in env.data.vertices
        if floor < v.co.z < lm["wrist_z"] and abs(v.co.x - sx) < 0.08
    ]


def finger_axis(env, lm, side):
    """Mesh finger axis: (finger-centroid, direction) or (None, reason)."""
    verts = finger_slab(env, lm, side)
    if len(verts) < 50:
        return None, "too few hand-slab verts"
    zs = sorted(v.z for v in verts)
    cut = zs[len(zs) // 2]
    up = [v for v in verts if v.z >= cut]
    lo = [v for v in verts if v.z < cut]
    if not up or not lo:
        return None, "empty hand-slab half"
    vec = centroid(lo) - centroid(up)
    if vec.length < 1e-9 or abs(vec.normalized().z) < 1e-6:
        return None, "degenerate finger axis"
    return (centroid(lo), vec.normalized()), None


def mesh_finger_dir(env, lm, side):
    """Mesh finger direction: centroid split of hand-slab verts, else None."""
    axis, _ = finger_axis(env, lm, side)
    return axis[1] if axis else None


def chain_angle(mesh_dir, head_b, tail_b):
    vec = tail_b.tail - head_b.head
    if vec.length < 1e-9:
        return None
    return math.degrees(mesh_dir.angle(vec.normalized()))


def _biped_only(lm):
    return (lm or {}).get("kind", "biped") == "biped"


def report_fingers(meta, env, eval_env, lm):
    """Warn-only: finger-chain angles + fingertip containment.

    Angles catch wrong-direction chains; containment catches a fan
    sitting sideways of the mesh fingers (eyeballed on Andras: old
    angle-only gate green while fingertips floated outside the mesh).
    Per-finger lateral-vs-axis vectors name the nudge but never gate:
    they cannot separate a healthy fit from a bad one (see note above).
    """
    if not _biped_only(lm):
        na = {"status": "no-data", "reason": "quadruped landmarks"}
        return {"L": dict(na), "R": dict(na)}
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    out = {}
    for side in SIDES:
        axis, reason = finger_axis(env, lm, side)
        if axis is None:
            out[side] = {"status": "no-data", "reason": reason}
            continue
        loc, mesh_dir = axis
        worst, worst_finger, angles = 0.0, None, {}
        for finger in (*FINGERS, "thumb"):
            head_b = bones.get(f"{finger}.01.{side}")
            tail_b = bones.get(f"{finger}.03.{side}")
            if head_b is None or tail_b is None:
                continue
            deg = chain_angle(mesh_dir, head_b, tail_b)
            if deg is None:
                continue
            angles[finger] = round(deg, 1)
            if deg > worst:
                worst, worst_finger = deg, finger
        if worst_finger is None:
            out[side] = {"status": "no-data", "reason": "finger bones missing"}
            continue
        lateral, worst_pos, worst_off = {}, 0.0, None
        for finger in FINGERS:
            tail_b = bones.get(f"{finger}.03.{side}")
            if tail_b is None:
                continue
            tip = mat @ tail_b.tail
            # Mesh-axis point at the tip's height, then the offset vector
            # (diagnostic: its x/y names the nudge).
            anchor = loc + mesh_dir * ((tip.z - loc.z) / mesh_dir.z)
            off = tip - anchor
            lat = math.hypot(off.x, off.y)
            held = point_depth(eval_env, tip)
            lateral[finger] = {
                "tip": [round(v, 4) for v in tip],
                "off": [round(v, 4) for v in off],
                "lateral": round(lat, 4),
                "inside": held["inside"],
                "votes": held["votes"],
                "depth": round(held["depth"], 4),
            }
            if lat > worst_pos:
                worst_pos, worst_off = lat, finger
        slab = finger_slab(env, lm, side)
        thumb_b = bones.get(f"thumb.03.{side}")
        thumb_near = (
            round(min((v - (mat @ thumb_b.tail)).length for v in slab), 4)
            if thumb_b is not None
            else None
        )
        outsiders = [f for f in FINGERS if not lateral.get(f, {}).get("inside", True)]
        angle_bad = worst > FINGER_WARN_DEG
        status = "warn" if (angle_bad or outsiders) else "ok"
        out[side] = {
            "status": status,
            "mesh_dir": [round(v, 3) for v in mesh_dir],
            "angles_deg": angles,
            "worst": worst_finger,
            "worst_deg": round(worst, 1),
            "lateral": lateral,
            "worst_pos": worst_off,
            "worst_pos_m": round(worst_pos, 4),
            "thumb_near": thumb_near,
            "warn_cause": (
                ("angle " if angle_bad else "")
                + ("outside:" + ",".join(outsiders) if outsiders else "")
            ).strip()
            or None,
        }
    return out


def foot_dir(foot, lm):
    """Mesh foot direction: front-half centroid minus back-half centroid."""
    ys = sorted(v.y for v in foot)
    med = ys[len(ys) // 2]
    if lm["facing"] == "-Y":
        fore = [v for v in foot if v.y <= med]
        back = [v for v in foot if v.y > med]
    else:
        fore = [v for v in foot if v.y >= med]
        back = [v for v in foot if v.y < med]
    if len(fore) < 10 or len(back) < 10:
        return None
    vec = centroid(fore) - centroid(back)
    return vec.normalized() if vec.length > 1e-9 else None


def yaw_deg(a, b):
    """Angle between the horizontal (x/y) components of two vectors."""
    ax, ay = a.x, a.y
    bx, by = b.x, b.y
    na, nb = math.hypot(ax, ay), math.hypot(bx, by)
    if na < 1e-9 or nb < 1e-9:
        return None
    cos = (ax * bx + ay * by) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def report_toes(meta, env, eval_env, lm):
    """Warn-only: overshoot + toe-axis yaw vs foot dir + tip shortfall."""
    if not _biped_only(lm):
        return {"status": "no-data", "reason": "quadruped landmarks"}
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    if lm["facing"] not in ("-Y", "+Y"):
        return {"status": "no-data", "reason": f"facing {lm['facing']!r}"}
    foot = [v.co for v in env.data.vertices if v.co.z < lm["ankle_z"] + 0.01]
    if not foot:
        return {"status": "no-data", "reason": "no sub-ankle verts"}
    front = min(v.y for v in foot) if lm["facing"] == "-Y" else max(v.y for v in foot)
    mesh_dir = foot_dir(foot, lm)
    out = {
        "foot_front": round(front, 4),
        "mesh_dir": [round(v, 3) for v in mesh_dir] if mesh_dir else None,
    }
    for side in SIDES:
        b = bones.get(f"toe.{side}")
        if b is None:
            out[side] = {"status": "no-data", "reason": "bone missing"}
            continue
        tip = mat @ b.tail
        over = front - tip.y if lm["facing"] == "-Y" else tip.y - front
        tip_pt = point_depth(eval_env, tip)
        axis = (b.tail - b.head).normalized()
        yaw = yaw_deg(axis, mesh_dir) if mesh_dir else None
        deg3 = (
            math.degrees(mesh_dir.angle(axis))
            if mesh_dir and axis.length > 1e-9
            else None
        )
        short = -over  # positive: tip falls short of the mesh foot front
        over_bad = over > TOE_WARN
        dir_bad = yaw is not None and yaw > TOE_DIR_WARN
        cause = ("overshoot " if over_bad else "") + ("direction" if dir_bad else "")
        status = "warn" if (over_bad or dir_bad) else "ok"
        out[side] = {
            "status": status,
            "tip": [round(v, 4) for v in tip],
            "overshoot": round(over, 4),
            "tip_inside": tip_pt["inside"],
            "yaw_deg": round(yaw, 1) if yaw is not None else None,
            "dir3_deg": round(deg3, 1) if deg3 is not None else None,
            "shortfall": round(short, 4),
            "warn_cause": cause.strip() or None,
        }
    return out


def head_bands(signature, lm):
    """Top-down (index, band) of single-blob slices above the neck."""
    top_down = list(reversed(signature))
    bands = [(i, s) for i, s in enumerate(top_down) if len(s["blobs"]) == 1]
    return [(i, s) for i, s in bands if s["z"] > lm["neck_z"]]


def skullcap_z(signature, lm):
    """Skullcap from head-blob width: widest ring above the neck dip + r."""
    bands = head_bands(signature, lm)
    if len(bands) < 6:
        return None, "fewer than 6 count==1 bands above the neck"
    radii = [s["blobs"][0]["r"] for _, s in bands]
    half = len(bands) // 2
    dip = half + min(range(len(bands) - half), key=radii[half:].__getitem__)
    if dip >= len(bands) - 1:
        # Monotonic narrowing into the neck (no trapezius flare above
        # neck_z): the bottom band is the neck-ward end, everything
        # above it is head. (A bottom flare would read wider, not min.)
        dip = len(bands) - 1
    if dip < 2:
        return None, "neck dip at the head-run edge"
    above = radii[:dip]
    wide = max(range(dip), key=above.__getitem__)
    cap = bands[wide][1]["z"] + above[wide]
    return round(cap, 4), None


def report_head(meta, signature, lm):
    """Warn-only: head-top vs skullcap (width-based, hair-proof)."""
    if not _biped_only(lm):
        return {"status": "no-data", "reason": "quadruped landmarks"}
    bones = meta.data.edit_bones
    head_bone = bones.get("spine.006")
    if head_bone is None:
        return {"status": "no-data", "reason": "spine.006 missing"}
    head_top = round((meta.matrix_world @ head_bone.tail).z, 4)
    cap, reason = skullcap_z(signature, lm)
    if cap is None:
        return {"status": "no-data", "reason": reason, "head_top": head_top}
    delta = round(head_top - cap, 4)
    return {
        "status": "warn" if abs(delta) > HEAD_WARN else "ok",
        "head_top": head_top,
        "skullcap_z": cap,
        "delta": delta,
        "zmax_spike": round(lm["zmax"] - cap, 4),
    }


FACE_PARTS = ("lid.T.L", "brow.T.L", "nose", "chin", "jaw", "ear.L")


def face_bones(bones):
    """Edit bones under the face root, or None if the rig has no face."""
    root = bones.get("face")
    if root is None:
        return None
    found = []

    def walk(bone):
        for child in bone.children:
            found.append(child)
            walk(child)

    walk(root)
    return found


def report_face(meta, signature, lm):
    """Warn-only heuristic: face bones placed within the head blob.

    The centroid must sit between a chin floor (lowest single-blob band
    above the neck: the jaw/neck junction, a floor rather than a chin
    landmark) and the skullcap, and the face bottom must clear the chin
    floor; anything else is a gross misplacement. Brow clearance, face
    frac, and per-part containment are reported, never gated (see the
    threshold note above): slices cannot see mesh features and cannot
    separate hair from skull.
    """
    if not _biped_only(lm):
        return {"status": "no-data", "reason": "quadruped landmarks"}
    bones = meta.data.edit_bones
    found = face_bones(bones)
    if not found:
        return {"status": "no-data", "reason": "face bones missing"}
    mat = meta.matrix_world
    mids = [mat @ ((b.head + b.tail) / 2) for b in found]
    top = max((mat @ b.head).z for b in found)
    top = max(top, max((mat @ b.tail).z for b in found))
    bot = min((mat @ b.head).z for b in found)
    bot = min(bot, min((mat @ b.tail).z for b in found))
    face_c = centroid(mids)
    cap, reason = skullcap_z(signature, lm)
    if cap is None:
        return {"status": "no-data", "reason": reason}
    bands = head_bands(signature, lm)
    chin_floor = round(min(s["z"] for _, s in bands), 4)
    brow = round(cap - top, 4)
    span = cap - lm["neck_z"]
    frac = round((face_c.z - lm["neck_z"]) / span, 3) if span > 0 else None
    outside = not (chin_floor < face_c.z < cap)
    sunk = bot < chin_floor
    cause = ("outside " if outside else "") + ("sunk" if sunk else "")
    status = "warn" if (outside or sunk) else "ok"
    parts = {}
    for name in FACE_PARTS:
        b = bones.get(name)
        if b is None:
            continue
        mid = mat @ ((b.head + b.tail) / 2)
        ring = min(bands, key=lambda pair: abs(pair[1]["z"] - mid.z))[1]
        blob = ring["blobs"][0]
        radial = math.hypot(mid.x - blob["cx"], mid.y - blob["cy"])
        parts[name] = {
            "z": round(mid.z, 4),
            "ring_z": round(ring["z"], 4),
            "ring_r": round(blob["r"], 4),
            "radial": round(radial, 4),
        }
    return {
        "status": status,
        "centroid": [round(v, 4) for v in face_c],
        "face_top": round(top, 4),
        "face_bottom": round(bot, 4),
        "chin_floor": chin_floor,
        "skullcap_z": cap,
        "brow_clearance": brow,
        "face_frac": frac,
        "parts": parts,
        "warn_cause": cause.strip() or None,
    }


QUAD_HOOVES = (
    ("FL", "f_hoof.L"),
    ("FR", "f_hoof.R"),
    ("BL", "r_hoof.L"),
    ("BR", "r_hoof.R"),
)
HOOF_GROUND_WARN = 0.05  # hoof tip |dz| past this warns (stance check)


def report_quad_head(meta, eval_env):
    """Warn-only: head-chain placement vs the mesh (mid/tip containment)."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    out = {}
    head_b = bones.get("head")
    skull_b = bones.get("skull")
    if head_b is None:
        return {"status": "no-data", "reason": "head bone missing"}
    mid = mat @ ((head_b.head + head_b.tail) / 2)
    held = point_depth(eval_env, mid)
    out["head_mid"] = {
        "inside": held["inside"],
        "votes": held["votes"],
        "depth": round(held["depth"], 4),
    }
    if skull_b is not None:
        for which, pt in (
            ("mid", mat @ ((skull_b.head + skull_b.tail) / 2)),
            ("tip", mat @ skull_b.tail),
        ):
            h = point_depth(eval_env, pt)
            out[f"skull_{which}"] = {
                "inside": h["inside"],
                "votes": h["votes"],
                "depth": round(h["depth"], 4),
            }
    out["status"] = "warn" if not held["inside"] else "ok"
    out["warn_cause"] = None if held["inside"] else "head-mid-outside"
    return out


def report_quad_tail(meta, eval_env, lm, tail_gated):
    """Warn-only tail placement; 'unverified' with fallback tail geometry."""
    if not tail_gated:
        return {"status": "unverified", "reason": "no tail geometry (fallback base)"}
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    per, outsiders = {}, []
    for name in STALKER_TAIL:
        b = bones.get(name)
        if b is None:
            continue
        held = point_depth(eval_env, mat @ ((b.head + b.tail) / 2))
        per[name] = {
            "inside": held["inside"],
            "votes": held["votes"],
            "depth": round(held["depth"], 4),
        }
        if not held["inside"]:
            outsiders.append(name)
    return {
        "status": "warn" if outsiders else "ok",
        "bones": per,
        "warn_cause": ("outside:" + ",".join(outsiders)) if outsiders else None,
    }


def report_quad_toes(meta, eval_env):
    """Warn-only: toe/hoof chain midpoint containment (never gated)."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    per, outsiders = {}, []
    for name in STALKER_TOES:
        b = bones.get(name)
        if b is None:
            continue
        held = point_depth(eval_env, mat @ ((b.head + b.tail) / 2))
        per[name] = {
            "inside": held["inside"],
            "votes": held["votes"],
            "depth": round(held["depth"], 4),
        }
        if not held["inside"]:
            outsiders.append(name)
    return {
        "status": "warn" if outsiders else "ok",
        "bones": per,
        "warn_cause": ("outside:" + ",".join(outsiders)) if outsiders else None,
    }


def report_quad_hooves(meta, lm):
    """Warn-only stance check: hoof tips at ground level (signed dz)."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    zmin = (lm.get("bounds") or {}).get("zmin")
    if zmin is None:
        return {"status": "no-data", "reason": "landmarks lack bounds.zmin"}
    per, worst, worst_leg = {}, 0.0, None
    for leg, bone_name in QUAD_HOOVES:
        b = bones.get(bone_name)
        if b is None:
            continue
        dz = round((mat @ b.tail).z - zmin, 4)
        per[leg] = dz
        if abs(dz) > abs(worst):
            worst, worst_leg = dz, leg
    status = "warn" if abs(worst) > HOOF_GROUND_WARN else "ok"
    return {
        "status": status,
        "dz": per,
        "worst": worst_leg,
        "worst_dz": round(worst, 4),
        "warn_cause": f"{worst_leg} {worst:.4f} m off ground"
        if status == "warn"
        else None,
    }


def run_gate(env, meta, lm, signature, margin=MARGIN_DEFAULT, open_allow=()):
    """Run the gate on in-session objects; returns the report dict.

    env: fused-exterior envelope mesh (inside checks need one closed
    shell; stacked production shells would flip parity). meta: metarig
    to validate. No scene changes except edit-mode/object-mode flips
    on meta. Pure report — verdict mapping and exit codes live in main().
    """
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    eval_env = env.evaluated_get(deps)

    drivers, tail_gated = drivers_for(meta, lm)
    closure = check_closure(meta, drivers)
    gaps = {k: round(v, 6) for k, v in closure["gaps"].items()}
    unlisted = [b for b in closure["open_drivers"] if b not in open_allow]
    closure_ok = not gaps and not unlisted

    stats, inside_fail = check_inside(meta, eval_env, margin, drivers)
    sym = check_symmetry(meta)
    sym_ok = sym["worst"] < SYMMETRY_TOL

    failures = []
    for bone, gap in sorted(gaps.items()):
        failures.append(
            f"closure: {bone} gap {gap:.6f} >= {CLOSE_EPS} — re-run "
            "reconnect_from; if intentional pass --open " + bone
        )
    for bone in unlisted:
        failures.append(
            f"open: {bone} is an open driver joint not listed via --open — "
            "close the joint or list it as intentional"
        )
    for item in inside_fail:
        if item.get("reason"):
            failures.append(f"missing driver bone {item['bone']} in metarig")
        elif not item["inside"]:
            failures.append(
                f"inside: {item['bone']} midpoint OUTSIDE the mesh "
                f"(parity {item['votes']}/6, nearest surface "
                f"{item['depth']:.4f} m) — check the chain's targets"
            )
        else:
            failures.append(
                f"margin: {item['bone']} midpoint {item['depth']:.4f} m from "
                f"the surface (< {margin:.4f} m margin) — silhouette-breach "
                "risk, nudge inward or re-fit"
            )
    if not sym_ok:
        failures.append(
            f"symmetry: {sym['pair']}/R differ by {sym['worst']:.4f} m "
            f"(>= {SYMMETRY_TOL}) — re-mirror .R from .L"
        )

    depths = [s["mid"] for s in stats.values()]
    worst_bone = min(stats, key=lambda n: stats[n]["mid"]) if stats else None
    is_quad = (lm or {}).get("kind") == "quadruped"
    warns = (
        {
            "quad_head": report_quad_head(meta, eval_env),
            "quad_tail": report_quad_tail(meta, eval_env, lm, tail_gated),
            "quad_toes": report_quad_toes(meta, eval_env),
            "quad_hooves": report_quad_hooves(meta, lm),
        }
        if is_quad
        else {
            "fingers": report_fingers(meta, env, eval_env, lm),
            "toes": report_toes(meta, env, eval_env, lm),
            "head": report_head(meta, signature, lm),
            "face": report_face(meta, signature, lm),
        }
    )
    report = {
        "margin": margin,
        "preset": "hll_stalker" if is_quad else "hll_hero",
        "tail_gated": tail_gated,
        "closure": {
            "max_gap": round(closure["max_gap"], 6),
            "gaps": gaps,
            "open_drivers": closure["open_drivers"],
            "open_allow": sorted(open_allow),
            "ok": closure_ok,
        },
        "inside": {
            "bones": stats,
            "failures": inside_fail,
            "worst_bone": worst_bone,
            "min_depth": round(min(depths), 4) if depths else 0.0,
            "mean_depth": round(sum(depths) / len(depths), 4) if depths else 0.0,
            "checked": len(stats),
            "ok": not inside_fail,
        },
        "symmetry": {
            "worst": round(sym["worst"], 6),
            "pair": sym["pair"],
            "ok": sym_ok,
        },
        "warns": warns,
        "overlay": None,
        "verdict": "pass" if not failures else "fail",
        "failures": failures,
    }
    bpy.ops.object.mode_set(mode="OBJECT")
    return report


def render_overlays(env, meta, lm, out_dir, drivers=DRIVER_BONES):
    """Front/side workbench PNGs + camera-projected driver joints JSON."""
    os.makedirs(out_dir, exist_ok=True)
    bpy.context.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode="OBJECT")
    scene = bpy.context.scene
    render = scene.render
    prev = (
        render.engine,
        render.resolution_x,
        render.resolution_y,
        render.film_transparent,
        render.filepath,
    )
    render.engine = "BLENDER_WORKBENCH"
    render.resolution_x = render.resolution_y = 480
    render.film_transparent = False

    corner = [Vector(c) for c in env.bound_box]
    center = sum(corner, Vector()) / 8
    size = max(
        max(c.x for c in corner) - min(c.x for c in corner),
        max(c.y for c in corner) - min(c.y for c in corner),
        max(c.z for c in corner) - min(c.z for c in corner),
    )
    cam_data = bpy.data.cameras.new("rigforge_validate_cam")
    cam = bpy.data.objects.new("rigforge_validate_cam", cam_data)
    scene.collection.objects.link(cam)
    dist = (size / 2) / math.tan(cam_data.angle / 2) * 1.3
    front = (
        Vector((0.0, -1.0, 0.0)) if lm["facing"] == "-Y" else Vector((0.0, 1.0, 0.0))
    )
    views = {"front": front, "side": Vector((1.0, 0.0, 0.0))}
    paths, projections = {}, {}
    scene.camera = cam
    for view, direction in views.items():
        cam.location = center + direction * dist
        track = (center - cam.location).to_track_quat("-Z", "Y")
        cam.matrix_world = Matrix.Translation(cam.location) @ track.to_matrix().to_4x4()
        bpy.context.view_layer.update()
        path = os.path.join(out_dir, f"{view}.png")
        render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths[view] = path
        joints = {}
        mat = meta.matrix_world
        for name in drivers:
            b = meta.data.bones.get(name)
            if b is None:
                continue
            joints[name] = {
                which: [
                    round(v, 4)
                    for v in world_to_camera_view(scene, cam, mat @ getattr(b, which))[
                        :2
                    ]
                ]
                for which in ("head", "tail")
            }
        projections[view] = {
            "location": [round(v, 4) for v in cam.location],
            "angle": round(cam_data.angle, 4),
            "joints": joints,
        }
    joints_path = os.path.join(out_dir, "overlay_joints.json")
    with open(joints_path, "w", encoding="utf-8") as f:
        json.dump(projections, f, indent=2)
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data)
    (
        render.engine,
        render.resolution_x,
        render.resolution_y,
        render.film_transparent,
        render.filepath,
    ) = prev
    return {"front": paths["front"], "side": paths["side"], "joints": joints_path}


CLOSEUP_BONES = ("thigh", "shin", "foot", "toe", "heel.02")


def render_closeup(mesh, env, meta, lm, out_path, size=640):
    """Toe-region close-up PNG + projected bone-midpoint labels JSON.

    Armatures never reach final renders, so bones become red tubes
    (beveled curves) drawn in front of the opaque workbench mesh;
    labels cover the leg/foot/toe bones of both legs plus any other
    bone projecting into the frame (that is how strays get caught).
    Text is overlaid outside Blender (headless Blender has no font
    rasterizer); the JSON carries pixel coords, see
    docs/auto_placement.md for the command.
    """
    bpy.context.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    render = scene.render
    prev = (
        render.engine,
        render.resolution_x,
        render.resolution_y,
        render.film_transparent,
        render.filepath,
    )
    prev_shading = (scene.display.shading.light, scene.display.shading.color_type)
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    render.engine = "BLENDER_WORKBENCH"
    render.resolution_x = render.resolution_y = size
    render.film_transparent = False
    render.filepath = out_path

    toes = []
    for side in SIDES:
        b = meta.data.bones.get(f"toe.{side}")
        if b is not None:
            toes.append(meta.matrix_world @ ((b.head_local + b.tail_local) / 2))
    if not toes:
        fail("close-up needs the toe bones")
    center = sum(toes, Vector()) / len(toes) + Vector((0.0, 0.0, 0.09))

    cam_data = bpy.data.cameras.new("rigforge_closeup_cam")
    cam = bpy.data.objects.new("rigforge_closeup_cam", cam_data)
    scene.collection.objects.link(cam)
    front = (
        Vector((0.0, -1.0, 0.0)) if lm["facing"] == "-Y" else Vector((0.0, 1.0, 0.0))
    )
    dist = (0.50 / 2) / math.tan(cam_data.angle / 2)
    cam.location = center + front * dist
    track = (center - cam.location).to_track_quat("-Z", "Y")
    cam.matrix_world = Matrix.Translation(cam.location) @ track.to_matrix().to_4x4()
    scene.camera = cam
    bpy.context.view_layer.update()

    tube_mat = bpy.data.materials.new("rigforge_closeup_tube")
    tube_mat.diffuse_color = (1.0, 0.03, 0.03, 1.0)
    tubes = []
    mat = meta.matrix_world
    for b in meta.data.bones:
        curve = bpy.data.curves.new(f"rf_closeup_{b.name}", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.0045
        curve.bevel_resolution = 1
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        head = mat @ b.head_local
        tail = mat @ b.tail_local
        spline.points[0].co = (head.x, head.y, head.z, 1.0)
        spline.points[1].co = (tail.x, tail.y, tail.z, 1.0)
        obj = bpy.data.objects.new(f"rf_closeup_{b.name}", curve)
        obj.data.materials.append(tube_mat)
        obj.show_in_front = True
        scene.collection.objects.link(obj)
        tubes.append(obj)

    mesh.hide_render = True
    meta.hide_render = True
    bpy.context.view_layer.update()
    bpy.ops.render.render(write_still=True)

    labels = []
    mat = meta.matrix_world
    wanted = [f"{stem}.{side}" for stem in CLOSEUP_BONES for side in SIDES]
    ordered = [n for n in wanted if n in meta.data.bones]
    ordered += [b.name for b in meta.data.bones if b.name not in ordered]

    def project(point):
        co = world_to_camera_view(scene, cam, point)
        if 0.02 < co.x < 0.98 and 0.02 < co.y < 0.98:
            return [round(co.x * size), round((1.0 - co.y) * size)]
        return None

    for name in ordered:
        b = meta.data.bones[name]
        head = mat @ b.head_local
        tail = mat @ b.tail_local
        for at, point in (("mid", (head + tail) / 2), ("head", head), ("tail", tail)):
            px = project(point)
            if px is not None:
                labels.append({"bone": name, "at": at, "x": px[0], "y": px[1]})
                break
    stem, _ = os.path.splitext(out_path)
    labels_path = stem + "_labels.json"
    with open(labels_path, "w", encoding="utf-8") as f:
        json.dump({"png": out_path, "size": size, "labels": labels}, f, indent=2)

    for obj in tubes:
        curve = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.curves.remove(curve)
    bpy.data.materials.remove(tube_mat)
    mesh.hide_render = False
    meta.hide_render = False
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data)
    (
        render.engine,
        render.resolution_x,
        render.resolution_y,
        render.film_transparent,
        render.filepath,
    ) = prev
    scene.display.shading.light, scene.display.shading.color_type = prev_shading
    return {"png": out_path, "labels": labels_path, "size": size}


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) < 3 or args[0].startswith("-"):
        fail("usage: validate_fit.py MESH FITTED_METARIG.py LANDMARKS.json")
    mesh_path, meta_path, lm_path = args[0], args[1], args[2]
    margin = (
        float(args[args.index("--margin") + 1])
        if "--margin" in args
        else (MARGIN_DEFAULT)
    )
    render_dir = (
        args[args.index("--render") + 1]
        if "--render" in args
        else "/tmp/rigforge_validate_overlay"
    )
    want_render = "--no-render" not in args
    on_fail = args[args.index("--on-fail") + 1] if "--on-fail" in args else "stop"
    if on_fail not in ("stop", "preset"):
        fail(f"--on-fail must be stop or preset, got {on_fail!r}")
    open_allow = (
        tuple(args[args.index("--open") + 1].split(",")) if "--open" in args else ()
    )
    report_path = (
        args[args.index("--report") + 1]
        if "--report" in args
        else "/tmp/rigforge_validate_report.json"
    )

    with open(lm_path, encoding="utf-8") as f:
        lm = json.load(f)
    quad = lm.get("kind") == "quadruped"
    need = (
        ("facing", "length", "bounds", "head", "tail_base", "legs")
        if quad
        else (
            "facing",
            "height",
            "zmax",
            "neck_z",
            "wrist_z",
            "wrist_x",
            "hand_tip_z",
            "ankle_z",
        )
    )
    for key in need:
        if lm.get(key) is None:
            fail(f"landmark JSON missing {key}")

    dl = load_tool("rf_validate_dl", "detect_landmarks.py")
    try:
        bpy.ops.preferences.addon_enable(module="rigforge")
    except RuntimeError:
        fail("rigforge addon not found; set BLENDER_USER_SCRIPTS overlay")

    mesh = dl.import_mesh(mesh_path)
    env = dl.envelope_copy(mesh)
    signature, _, _ = dl.slice_signature(env, 120)
    meta = build_metarig_from_module(meta_path)
    if "--closeup" in args:
        arg = args[args.index("--closeup") + 1]
        closeup = render_closeup(mesh, env, meta, lm, arg)
        print("RIGFORGE_CLOSEUP_BEGIN")
        print(json.dumps(closeup, indent=2))
        print("RIGFORGE_CLOSEUP_OK")
        return
    report = run_gate(env, meta, lm, signature, margin, open_allow)
    report["subject"] = mesh_path
    report["metarig"] = meta_path
    if want_render:
        gate_drivers, _ = drivers_for(meta, lm)
        report["overlay"] = render_overlays(env, meta, lm, render_dir, gate_drivers)

    if report["verdict"] == "pass":
        report["ladder"] = "full_auto"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(
            f"gate PASS: {report['inside']['checked']} drivers inside "
            f"with margin, symmetry {report['symmetry']['worst']:.4f}"
        )
        print("RIGFORGE_VALIDATE_BEGIN")
        print(json.dumps(report, indent=2))
        print("RIGFORGE_VALIDATE_OK")
        return

    # Rung 1: stop the pipeline with bones and fixes (default).
    print(f"gate FAIL: {len(report['failures'])} failing check(s)")
    for line in report["failures"]:
        print("STOP:", line)
    if on_fail == "stop":
        report["ladder"] = "artist_confirm"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print("RIGFORGE_VALIDATE_BEGIN")
        print(json.dumps(report, indent=2))
        print("RIGFORGE_VALIDATE_FAIL")
        raise SystemExit(1)

    # Rung 2: stock preset as-is + its own report (explicit operator choice).
    bpy.ops.object.mode_set(mode="OBJECT")
    op = "rigforge_hll_stalker_metarig_add" if quad else "rigforge_hll_hero_metarig_add"
    getattr(bpy.ops.object, op)()
    preset = bpy.context.view_layer.objects.active
    fallback = run_gate(env, preset, lm, signature, margin, open_allow)
    fallback["subject"] = mesh_path
    fallback["metarig"] = f"stock {'hll_stalker' if quad else 'hll_hero'} preset as-is"
    report["ladder"] = "preset_as_is"
    report["fallback_preset"] = fallback
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(
        "fitted gate FAILED; rung 2: preset as-is + report "
        f"(preset verdict: {fallback['verdict']})"
    )
    print("RIGFORGE_VALIDATE_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_VALIDATE_PRESET")


if __name__ == "__main__":
    main()
