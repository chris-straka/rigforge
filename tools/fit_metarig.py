# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit the hll_hero metarig to a subject mesh (auto-placement Phase 2).

Headless (from the repo root; the preset + generate need the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/fit_metarig.py -- \\
      MESH LANDMARKS.json [--out FILE] [--blend FILE] [--no-generate]

Inputs: subject mesh (glb/fbx/obj, transforms applied + joined, same as
Phase 1) and the Phase-1 landmark JSON from tools/detect_landmarks.py.
Output: fitted metarig module in write_metarig format (default
/tmp/rigforge_fitted_hero.py), fit report JSON between
RIGFORGE_FIT_BEGIN/OK markers, and (default) an in-session headless
generate of the fitted metarig via the real generate path
(RIGFORGE_FIT_GENERATE_OK + DEF-bone count as proof).

Method (docs/auto_placement.md Section 4.3): coarse uniform scale to the
subject height, then a root-outward solve per driver chain (neck/head,
arm, leg): head onto the proximal target, tail onto the distal target,
children rigid-follow, rolls preserved unless a bone's direction changed
more than ~5 deg. Joint x/z come from landmark heights plus tracked limb
axes; y uses measured centroids only where sections are slim and
interpolated (arms, knee), the nearest band at the ankle, and preset
anatomy at torso-attached joints. .L is solved from landmark magnitudes
and .R is mirrored. Reuses tools/make_hll_presets.py machinery (descendants /
apply / about / reconnect_from) and tools/detect_landmarks.py analysis
(import_mesh / envelope_copy / slice_signature / tracking).

Section 6 findings addressed: (1) analysis runs on the envelope remesh
(stacked shells would read as phantom blobs); (2) the shoulder is
back-projected from the elbow along the tracked arm axis by the
preset's upper-arm length (the arm run starts at the armpit, below the
joint); (3) the ankle falls back to a sole offset (zmin + 0.044*height,
preset-derived ratio) when ankle_dip is weak (< 0.010) or the dip sits
implausibly high (> 0.085*height, i.e. mid-boot, not the joint) — same
pattern guards gauntlet wrists (hand_tip + palm length when wrist_dip
< 0.004); (4) arm tracking uses count==3 slices above the wrist only,
so finger splits below the palm never enter the axis fit.
"""

import importlib.util
import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

# --- Fit triggers (see module docstring; Andras values in docs) ---
ANKLE_DIP_MIN = 0.010  # below: boot hides the narrowing -> sole offset
ANKLE_MAX_Z_FRAC = 0.085  # above: dip is mid-boot, not the joint
ANKLE_SOLE_FRAC = 0.044  # preset shin-tail height / HERO_HEIGHT
WRIST_DIP_MIN = 0.004  # below: gauntlet hides wrist -> hand_tip + palm
ELBOW_CAUTION_DIP = 0.005  # below: keep the dip, but log a caution
LEG_LINE_MIN_Z_FRAC = 0.11  # leg axis fits above the foot flare
ROLL_RECOMPUTE_DEG = 5.0  # Section 4.3: preserve rolls below this
MIN_LINE_BANDS = 6
CLOSE_EPS = 1e-4  # same epsilon as reconnect_from
SYMMETRY_TOL = 0.01  # Section 4.4: L/R mirror error must be < 1 cm


def fail(msg):
    print("RIGFORGE_FIT_FAIL:", msg)
    raise SystemExit(msg)


def load_tool(name, filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fit_line(points):
    """Least squares x(z), y(z) over (x, y, z) samples; returns eval fn."""
    n = len(points)
    sz = sum(p[2] for p in points)
    szz = sum(p[2] ** 2 for p in points)
    denom = n * szz - sz * sz
    if denom == 0:
        fail("degenerate limb run: all bands share one z")
    out = {}
    for key, idx in (("x", 0), ("y", 1)):
        sv = sum(p[idx] for p in points)
        szv = sum(p[idx] * p[2] for p in points)
        slope = (n * szv - sz * sv) / denom
        out[key] = (slope, sv / n - slope * sz / n)
    (mx, bx), (my, by) = out["x"], out["y"]

    def at(z):
        return (mx * z + bx, my * z + by, z)

    at.slope = (mx, my)
    return at


def limb_samples(dl, top_down, bands):
    """Per-band averaged limb centers; skips bands where a side is lost."""
    samples, asym = [], 0.0
    for i in bands:
        blobs = top_down[i]["blobs"]
        r = dl.outer_blob(blobs, 1.0)
        left = dl.outer_blob(blobs, -1.0)
        if r is None or left is None:
            continue
        z = top_down[i]["z"]
        samples.append(
            ((abs(r["cx"]) + abs(left["cx"])) / 2, (r["cy"] + left["cy"]) / 2, z)
        )
        asym = max(asym, abs(abs(r["cx"]) - abs(left["cx"])))
    return samples, asym


def track_axes(dl, mesh_path, lm):
    """Envelope remesh + slice/blob analysis; returns arm/leg axis fns."""
    target = dl.envelope_copy(dl.import_mesh(mesh_path))
    signature, _, _ = dl.slice_signature(target, 120)
    top_down = list(reversed(signature))
    arms = dl.longest_run([i for i, s in enumerate(top_down) if len(s["blobs"]) >= 3])
    legs = dl.longest_run(
        [i for i, s in enumerate(top_down) if len(s["blobs"]) == 2 and i > max(arms)]
    )
    if not arms or not legs:
        fail("no biped signature for axis tracking")
    # Finding 4: arm axis from clean torso+2-arm slices above the wrist
    # only — finger splits below the palm are excluded.
    arm_bands = [
        i
        for i in arms
        if len(top_down[i]["blobs"]) == 3 and top_down[i]["z"] > lm["wrist_z"]
    ]
    if len(arm_bands) < MIN_LINE_BANDS:
        arm_bands = [i for i in arms if len(top_down[i]["blobs"]) == 3]
    leg_min_z = LEG_LINE_MIN_Z_FRAC * lm["height"]
    leg_bands = [i for i in legs if top_down[i]["z"] > leg_min_z]
    if len(leg_bands) < MIN_LINE_BANDS:
        leg_bands = legs
    arm_samples, arm_asym = limb_samples(dl, top_down, arm_bands)
    leg_samples, leg_asym = limb_samples(dl, top_down, leg_bands)
    if len(arm_samples) < MIN_LINE_BANDS or len(leg_samples) < MIN_LINE_BANDS:
        fail("untrackable limb blobs for axis fit")
    # Full-run leg centroids for the ankle y lookup (the fit region ends
    # above the foot flare, and cy must not be extrapolated past it).
    leg_cy = [(z, cy) for _, cy, z in limb_samples(dl, top_down, legs)[0]]
    return {
        "arm": fit_line(arm_samples),
        "leg": fit_line(leg_samples),
        "arm_bands": len(arm_samples),
        "leg_bands": len(leg_samples),
        "leg_cy": leg_cy,
        "input_asym": round(max(arm_asym, leg_asym), 4),
    }


def nearest_cy(leg_cy, z):
    if not leg_cy:
        fail("no leg centroid bands for ankle y lookup")
    return min(leg_cy, key=lambda zcy: abs(zcy[0] - z))[1]


def resolve_targets(lm, axes, preset):
    """Landmark JSON + axis fits -> driver-joint targets + fallback record."""
    if lm["facing"] not in ("-Y", "+Y"):
        fail(f"unsupported facing {lm['facing']!r}")
    for a, b in (
        ("neck_z", "armpit_z"),
        ("armpit_z", "elbow_z"),
        ("elbow_z", "wrist_z"),
        ("wrist_z", "hand_tip_z"),
        ("crotch_z", "knee_z"),
    ):
        if not lm[a] > lm[b]:
            fail(f"landmark order violated: {a}={lm[a]} <= {b}={lm[b]}")
    s = lm["height"] / preset["height"]
    arm, leg = axes["arm"], axes["leg"]
    cautions = []

    elbow = Vector(arm(lm["elbow_z"]))
    wrist = Vector(arm(lm["wrist_z"]))
    if lm["elbow_dip"] < ELBOW_CAUTION_DIP:
        cautions.append(f"elbow_dip {lm['elbow_dip']} < {ELBOW_CAUTION_DIP}")

    # Finding 3 (wrist): gauntlets hide the narrowing; fall back to the
    # trusted fingertip floor plus the preset's palm length.
    wrist_fb = lm["wrist_dip"] < WRIST_DIP_MIN
    if wrist_fb:
        wz = lm["hand_tip_z"] + preset["palm_drop"] * s
        wrist = Vector((*arm(wz)[:2], wz))
        cautions.append(f"wrist fallback to hand_tip offset: z={wz:.4f}")
    if lm["wrist_dip"] < ANKLE_DIP_MIN and not wrist_fb:
        cautions.append(f"wrist_dip {lm['wrist_dip']} weak but usable")

    # Finding 2: back-project the shoulder from the elbow along the arm
    # axis by the preset's upper-arm length; the arm run starts at the
    # armpit, well below the joint. The upper arm holds elbow y (flat in
    # the tracked bands) instead of continuing the forearm's slope.
    axis_up = (elbow - wrist).normalized()
    shoulder = elbow + axis_up * preset["upper_len"] * s
    shoulder.y = elbow.y
    min_shoulder_z = lm["armpit_z"] + 0.02
    if shoulder.z < min_shoulder_z:
        t = (min_shoulder_z - elbow.z) / axis_up.z
        shoulder = elbow + axis_up * t
        shoulder.y = elbow.y
        cautions.append("shoulder clamped above armpit")

    # Finding 3 (ankle): boots hide the joint; fall back to sole offset
    # when the dip is weak or sits implausibly high (mid-boot).
    weak = lm["ankle_dip"] < ANKLE_DIP_MIN
    high = lm["ankle_z"] > ANKLE_MAX_Z_FRAC * lm["height"]
    ankle_fb = weak or high
    ankle_z = lm["zmin"] + ANKLE_SOLE_FRAC * lm["height"] if ankle_fb else lm["ankle_z"]
    # Ankle y is sampled at the ankle band: the leg line ends above the
    # foot flare, and front/back centroids must not be extrapolated past
    # the calf into the foot (or above the crotch into the pelvis, so the
    # pelvis-attached hip keeps the preset's y).
    ankle = Vector((leg(ankle_z)[0], nearest_cy(axes["leg_cy"], ankle_z), ankle_z))
    knee = Vector(leg(lm["knee_z"]))
    hip = Vector(
        (leg(preset["hip_z"] * s)[0], preset["hip_y"] * s, preset["hip_z"] * s)
    )

    hand_vec = preset["hand_vec"] * s
    foot_vec = preset["foot_vec"] * s
    if lm["facing"] == "+Y":
        foot_vec.y *= -1
    targets = {
        "shoulder": shoulder,
        "elbow": elbow,
        "wrist": wrist,
        "hand": wrist + hand_vec,
        "hip": hip,
        "knee": knee,
        "ankle": ankle,
        "foot": ankle + foot_vec,
    }
    fallbacks = {
        "ankle": {
            "fired": ankle_fb,
            "weak_dip": weak,
            "implausible_z": high,
            "measured_z": lm["ankle_z"],
            "used_z": round(ankle_z, 4),
        },
        "wrist": {
            "fired": wrist_fb,
            "dip": lm["wrist_dip"],
            "used_z": round(wrist.z, 4),
        },
    }
    return targets, fallbacks, cautions, s


DRIVERS = (
    "upper_arm.L",
    "forearm.L",
    "hand.L",
    "thigh.L",
    "shin.L",
    "foot.L",
)


def snapshot_preset(hp, obj):
    """Preset measures in OBJECT mode (Bone head_local/matrix_local)."""
    bones = obj.data.bones
    height = max(max(b.head_local.z, b.tail_local.z) for b in bones)
    fingers = hp.descendants(bones, "hand.L")
    fingertip = min(min(bones[n].head_local.z, bones[n].tail_local.z) for n in fingers)
    hand = bones["hand.L"]
    preset = {
        "height": height,
        "upper_len": (
            bones["upper_arm.L"].tail_local - bones["upper_arm.L"].head_local
        ).length,
        "hand_vec": hand.tail_local - hand.head_local,
        "foot_vec": bones["foot.L"].tail_local - bones["foot.L"].head_local,
        "palm_drop": hand.tail_local.z - fingertip,
        "hip_z": bones["thigh.L"].head_local.z,
        "hip_y": bones["thigh.L"].head_local.y,
    }
    snap = {}
    for name in DRIVERS:
        b = bones[name]
        snap[name] = {
            "head": b.head_local.copy(),
            "tail": b.tail_local.copy(),
            "dir": (b.tail_local - b.head_local).normalized(),
            "z_axis": b.matrix_local.col[2].to_3d().normalized(),
        }
    return preset, snap


def solve_bone(hp, align_roll, obj, snap, name, head_t, tail_t):
    """Root-outward driver solve: head on target, tail aimed then set.

    Children rigid-follow (rotation about the head, then the length
    residual); roll is preserved unless the direction changed by more
    than ROLL_RECOMPUTE_DEG, in which case the bone's z-axis is
    realigned to its pre-fit world orientation.
    """
    bones = obj.data.edit_bones
    sub = hp.descendants(bones, name)
    hp.apply(bones, sub, Matrix.Translation(head_t - bones[name].head))
    cur = bones[name].tail - bones[name].head
    want = tail_t - head_t
    if cur.length < 1e-9 or want.length < 1e-9:
        fail(f"degenerate solve for {name}")
    rot = cur.rotation_difference(want).to_matrix().to_4x4()
    hp.apply(bones, sub, hp.about(head_t, rot))
    residual = tail_t - bones[name].tail
    bones[name].tail = tail_t
    hp.apply(bones, [n for n in sub if n != name], Matrix.Translation(residual))
    new_dir = (bones[name].tail - bones[name].head).normalized()
    angle_deg = math.degrees(snap[name]["dir"].angle(new_dir))
    realigned = False
    if angle_deg > ROLL_RECOMPUTE_DEG:
        align_roll(obj, name, snap[name]["z_axis"])
        realigned = True
    return angle_deg, realigned


def mirror_subtree(hp, bones, root_L):
    """Copy a solved .L subtree onto .R (x negated, roll negated)."""
    for name in hp.descendants(bones, root_L):
        if not name.endswith(".L"):
            fail(f"cannot mirror non-paired bone {name}")
        rname = name[:-2] + ".R"
        if rname not in bones:
            fail(f"missing mirror bone {rname}")
        src, dst = bones[name], bones[rname]
        dst.head = (-src.head.x, src.head.y, src.head.z)
        dst.tail = (-src.tail.x, src.tail.y, src.tail.z)
        dst.roll = -src.roll


def fit_spine(hp, obj, lm):
    """Torso compressed so the neck base lands on neck_z, head on zmax.

    The head rig requires spine.004 coincident with spine.003.tail, so
    the neck delta is distributed across spine..spine.003 (z-scale about
    the pelvis base) instead of detaching the neck; then the neck chain
    is translated onto the new chest top (connected) and stretched to
    the head top. Face bones rigid-follow the head (Section 4.3).
    """
    bones = obj.data.edit_bones
    base = bones["spine"].head.z
    old_top = bones["spine.003"].tail.z
    old_neck_z = bones["spine.004"].head.z
    if old_top <= base:
        fail("torso span collapsed")
    k_torso = (lm["neck_z"] - base) / (old_top - base)
    torso = Matrix.Identity(4)
    torso[2][2] = k_torso
    hp.apply(
        bones,
        ["spine", "spine.001", "spine.002", "spine.003"],
        hp.about(Vector((0.0, 0.0, base)), torso),
    )
    neck = hp.descendants(bones, "spine.004")
    hp.apply(
        bones,
        neck,
        Matrix.Translation(bones["spine.003"].tail - bones["spine.004"].head),
    )
    dz = lm["neck_z"] - old_neck_z
    top = bones["spine.006"].tail.z
    span = top - lm["neck_z"]
    if span <= 0:
        fail("neck span collapsed")
    k = (lm["zmax"] - lm["neck_z"]) / span
    stretch = Matrix.Identity(4)
    stretch[2][2] = k
    pivot = Vector((0.0, 0.0, lm["neck_z"]))
    chain = ["spine.004", "spine.005", "spine.006"]
    old_head = bones["spine.006"].head.copy()
    hp.apply(bones, chain, hp.about(pivot, stretch))
    shift = bones["spine.006"].head - old_head
    hp.apply(bones, hp.descendants(bones, "face"), Matrix.Translation(shift))
    return dz, k, k_torso


def joint_positions(bones, edit):
    h, t = ("head", "tail") if edit else ("head_local", "tail_local")

    def at(bone, which):
        return getattr(bones[bone], which)

    return {
        "shoulder": at("upper_arm.L", h),
        "elbow": at("upper_arm.L", t),
        "wrist": at("forearm.L", t),
        "hand": at("hand.L", t),
        "hip": at("thigh.L", h),
        "knee": at("thigh.L", t),
        "ankle": at("shin.L", t),
        "foot": at("foot.L", t),
    }


def emit_module(obj, out_path, mesh_path, lm_path):
    from rigforge.utils.rig import write_metarig

    code = write_metarig(
        obj, layers=True, func_name="create", groups=True, widgets=True
    )
    header = (
        "# SPDX-License-Identifier: GPL-2.0-or-later\n"
        "# Fitted metarig, generated by tools/fit_metarig.py from hll_hero\n"
        f"# Subject: {os.path.basename(mesh_path)} | landmarks: "
        f"{os.path.basename(lm_path)} — do not hand-edit, re-fit instead.\n\n"
    )
    compile(header + code, out_path, "exec")  # emitted module must parse
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + code + "\n")
    return out_path


def check_closure(obj):
    bones = obj.data.edit_bones
    max_gap, open_drivers = 0.0, []
    for b in bones:
        if b.parent is None:
            continue
        gap = (b.head - b.parent.tail).length
        if b.use_connect:
            max_gap = max(max_gap, gap)
            if gap >= CLOSE_EPS:
                fail(f"joint left open: {b.name} gap={gap:.6f}")
        elif b.name in DRIVERS or b.name in ("spine.004", "face"):
            open_drivers.append(b.name)
    return max_gap, sorted(open_drivers)


def check_symmetry(bones):
    worst = 0.0
    for b in bones:
        if not b.name.endswith(".L"):
            continue
        other = bones.get(b.name[:-2] + ".R")
        if other is None:
            continue
        worst = max(
            worst,
            abs(b.head.x + other.head.x),
            abs(b.head.y - other.head.y),
            abs(b.head.z - other.head.z),
            abs(b.tail.x + other.tail.x),
            abs(b.tail.y - other.tail.y),
            abs(b.tail.z - other.tail.z),
        )
    if worst >= SYMMETRY_TOL:
        fail(f"symmetry error {worst:.4f} exceeds {SYMMETRY_TOL}")
    return worst


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) < 2 or args[0].startswith("-"):
        fail("usage: fit_metarig.py MESH LANDMARKS.json [--out F] [--blend F]")
    mesh_path, lm_path = args[0], args[1]
    out_path = (
        args[args.index("--out") + 1]
        if "--out" in args
        else "/tmp/rigforge_fitted_hero.py"
    )
    blend_path = (
        args[args.index("--blend") + 1]
        if "--blend" in args
        else "/tmp/rigforge_fitted_hero.blend"
    )
    want_generate = "--no-generate" not in args
    if "--no-blend" in args:
        blend_path = None

    with open(lm_path, encoding="utf-8") as f:
        lm = json.load(f)
    for key in (
        "facing",
        "height",
        "zmin",
        "zmax",
        "neck_z",
        "armpit_z",
        "elbow_z",
        "wrist_z",
        "hand_tip_z",
        "wrist_x",
        "crotch_z",
        "knee_z",
        "ankle_z",
        "ankle_x",
        "wrist_dip",
        "elbow_dip",
        "knee_dip",
        "ankle_dip",
    ):
        if lm.get(key) is None:
            fail(f"landmark JSON missing {key}")

    dl = load_tool("rf_detect_landmarks", "detect_landmarks.py")
    hp = load_tool("rf_make_hll_presets", "make_hll_presets.py")
    try:
        bpy.ops.preferences.addon_enable(module="rigforge")
    except RuntimeError:
        fail("rigforge addon not found; set BLENDER_USER_SCRIPTS overlay")
    import rigforge
    from rigforge.utils.bones import align_bone_z_axis

    print("rigforge:", rigforge.__file__)

    # Analysis space: joined mesh + envelope remesh (finding 1), same as
    # the detector, so landmark coordinates apply directly.
    axes = track_axes(dl, mesh_path, lm)

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.rigforge_hll_hero_metarig_add()
    obj = bpy.context.view_layer.objects.active
    preset, snap = snapshot_preset(hp, obj)
    targets, fallbacks, cautions, s = resolve_targets(lm, axes, preset)
    before = {k: v * s for k, v in joint_positions(obj.data.bones, False).items()}

    bpy.ops.object.mode_set(mode="EDIT")
    bones = obj.data.edit_bones
    for b in bones:  # as in make_hll_presets: no connected dragging
        b.use_connect = False
    hp.apply(bones, [b.name for b in bones], Matrix.Scale(s, 4))  # coarse fit
    spine_before = {
        "neck_base": bones["spine.004"].head.copy(),
        "head_top": bones["spine.006"].tail.copy(),
    }
    neck_dz, neck_k, torso_k = fit_spine(hp, obj, lm)

    # Arm: shift the shoulder subtree so the socket lands on the
    # back-projected joint, then solve outward; fingers rigid-follow.
    hp.apply(
        bones,
        hp.descendants(bones, "shoulder.L"),
        Matrix.Translation(targets["shoulder"] - bones["upper_arm.L"].head),
    )
    angles = {}
    for bone, head_k, tail_k in (
        ("upper_arm.L", "shoulder", "elbow"),
        ("forearm.L", "elbow", "wrist"),
        ("hand.L", "wrist", "hand"),
        ("thigh.L", "hip", "knee"),
        ("shin.L", "knee", "ankle"),
        ("foot.L", "ankle", "foot"),
    ):
        angles[bone], _ = solve_bone(
            hp, align_bone_z_axis, obj, snap, bone, targets[head_k], targets[tail_k]
        )
    realigned_bones = [b for b in angles if angles[b] > ROLL_RECOMPUTE_DEG]
    mirror_subtree(hp, bones, "shoulder.L")
    mirror_subtree(hp, bones, "thigh.L")
    hp.reconnect_from(obj, "rigforge_hll_hero_metarig_add")

    max_gap, open_drivers = check_closure(obj)
    bones = obj.data.edit_bones  # refetch: reconnect_from switched modes
    symmetry = check_symmetry(bones)
    after = joint_positions(bones, True)
    joints = {}
    for key, tgt in targets.items():
        joints[key] = {
            "target": [round(v, 4) for v in tgt],
            "before_d": round((before[key] - tgt).length, 4),
            "after_d": round((after[key] - tgt).length, 4),
        }
    joints["neck_base"] = {
        "target": [0.0, round(spine_before["neck_base"].y, 4), lm["neck_z"]],
        "before_d": round(abs(spine_before["neck_base"].z - lm["neck_z"]), 4),
        "after_d": round(abs(bones["spine.004"].head.z - lm["neck_z"]), 4),
    }
    joints["head_top"] = {
        "target": [0.0, round(spine_before["head_top"].y, 4), lm["zmax"]],
        "before_d": round(abs(spine_before["head_top"].z - lm["zmax"]), 4),
        "after_d": round(abs(bones["spine.006"].tail.z - lm["zmax"]), 4),
    }
    lengths = {}
    for bone in DRIVERS:
        old = (snap[bone]["tail"] - snap[bone]["head"]).length * s
        new = (bones[bone].tail - bones[bone].head).length
        lengths[bone] = round(100 * (new - old) / old, 1)

    emit_module(obj, out_path, mesh_path, lm_path)

    generate = None
    if want_generate:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = obj
        bpy.ops.pose.rigforge_generate()
        rig = next(
            (
                o
                for o in bpy.data.objects
                if o.type == "ARMATURE" and "rig_id" in o.data
            ),
            None,
        )
        if rig is None:
            fail("generate produced no rig")
        n_def = sum(1 for b in rig.data.bones if b.name.startswith("DEF-"))
        print(f"generate: rig bones={len(rig.data.bones)} def={n_def}")
        if n_def <= 30:
            fail(f"only {n_def} DEF bones")
        generate = {"rig_bones": len(rig.data.bones), "def_bones": n_def}
        print("RIGFORGE_FIT_GENERATE_OK")

    if blend_path:
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)

    report = {
        "subject": mesh_path,
        "facing": lm["facing"],
        "height": lm["height"],
        "scale": round(s, 4),
        "axis_bands": {"arm": axes["arm_bands"], "leg": axes["leg_bands"]},
        "input_asym": axes["input_asym"],
        "fallbacks": fallbacks,
        "cautions": cautions,
        "neck": {
            "dz": round(neck_dz, 4),
            "stretch": round(neck_k, 3),
            "torso_k": round(torso_k, 3),
        },
        "joints": joints,
        "length_change_pct": lengths,
        "roll_realigned": sorted(realigned_bones),
        "closure": {"max_gap": round(max_gap, 6), "open_drivers": open_drivers},
        "symmetry_max": round(symmetry, 6),
        "emit": out_path,
        "generate": generate,
    }
    print("RIGFORGE_FIT_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_FIT_OK")


if __name__ == "__main__":
    main()
