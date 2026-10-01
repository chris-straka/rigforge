# SPDX-License-Identifier: GPL-2.0-or-later
"""Fit an HLL metarig to a subject mesh (auto-placement Phase 2+).

Headless (from the repo root; the preset + generate need the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/fit_metarig.py -- \\
      MESH LANDMARKS.json [--out FILE] [--blend FILE] [--no-generate]
      [--preset hll_hero|hll_stalker] [--hints HINTS.json] [--hints-rotated]

--preset defaults from the landmarks kind (biped -> hll_hero,
quadruped -> hll_stalker) and must match it. --hints feeds a
unirig-joints/1 document as soft priors (agree -> trust, disagree ->
measurement wins + divergence report; see tools/unirig_hints.py).

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
back-projected from the elbow along the upper-arm ray (bands above
the elbow) by the preset's upper-arm length, mesh-verified with a
shortening march (the arm run starts at the armpit, below the joint;
the old forearm-direction projection overshot medially on hanging
arms); (3) the ankle falls back to a sole offset (zmin + 0.048*height)
when ankle_dip is weak (< 0.010), sits implausibly high
(> 0.085*height, i.e. mid-boot, not the joint), or clears the measured
forefoot top by more than 0.025 — same pattern guards gauntlet wrists
(hand_tip + palm length when wrist_dip < 0.004); (4) arm tracking uses
valid-pair slices above the wrist (any blob count; speck bands with
tiny outer blobs are dropped), so finger splits below the palm never
enter the axis fit while true upper-arm bands in count>3 slices do.
Section 9 follow-ups: the elbow is re-derived on the robust profile
when the detector's band was speck-dragged; the hand tail is mesh-fit
when the preset vector overshoots the fingers; foot/hand targets get
a bounded verify-nudge; finger chains are re-aimed onto the measured
mesh finger column (knuckle rotation capped at 60 deg, per-finger
revert on failed verify). Every correction is verify-then-correct:
prospective midpoints raycast with the gate's own point_depth, and
only failing bones deviate from the legacy solve.
"""

import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import detect_landmarks as _detect
from . import make_hll_presets as _presets
from . import unirig_hints as _hints
from . import validate_fit as _validate

# --- Fit triggers (see module docstring; Andras values in docs) ---
ANKLE_DIP_MIN = 0.010  # below: boot hides the narrowing -> sole offset
ANKLE_MAX_Z_FRAC = 0.085  # above: dip is mid-boot, not the joint
ANKLE_SOLE_FRAC = 0.048  # ankle joint above the sole (~8 cm at 1.7 m)
WRIST_DIP_MIN = 0.004  # below: gauntlet hides wrist -> hand_tip + palm
ELBOW_CAUTION_DIP = 0.005  # below: keep the dip, but log a caution
LEG_LINE_MIN_Z_FRAC = 0.11  # leg axis fits above the foot flare
ROLL_RECOMPUTE_DEG = 5.0  # Section 4.3: preserve rolls below this
MIN_LINE_BANDS = 6
CLOSE_EPS = 1e-4  # same epsilon as reconnect_from
SYMMETRY_TOL = 0.01  # Section 4.4: L/R mirror error must be < 1 cm
# --- Fitter follow-ups (docs/auto_placement.md Section 9): verify-then-
# correct. Prospective driver midpoints are raycast against the envelope
# with the GATE'S OWN verdict functions (validate_fit.point_depth, so the
# margin contract cannot drift); corrections apply ONLY to failing bones,
# so passing fits solve exactly as before. ---
MIN_BLOB_N = 12  # outer-blob crossing count below: speck, not a limb
MIN_BLOB_R = 0.012  # outer-blob radius below: speck, not a limb
ELBOW_OVERRIDE_MIN = 0.005  # adopt the re-derived elbow above this delta
UPPER_RAY_MIN_BANDS = 4  # bands above the elbow needed for the ray
SHOULDER_MARCH_MIN = 0.4  # back-projection shortens to at most this frac
SHOULDER_MARCH_STEP = 0.05
FOOT_TOP_MARGIN = 0.025  # ankle dip above forefoot top + this: not a joint
HAND_SPAN_FRAC = 0.75  # mesh-fit hand tail: knuckles above the tip floor
HAND_COL_MIN_BANDS = 3  # finger-column bands needed for the hand fit
NUDGE_STEP = 0.002  # verify-nudge: target step opposite the near surface
NUDGE_MAX_STEPS = 8
NUDGE_HEADROOM = 0.002  # nudge past the margin by this (remesh variance)
FINGER_SLAB_MEDIAL = 0.03  # finger slab: wrist_x - this (thigh side)
FINGER_SLAB_LATERAL = 0.08  # finger slab: wrist_x + this (splay side)
FINGER_SLAB_MIN_N = 50
THUMB_MASK_R = 0.015  # slab verts nearer the legacy thumb are thumb flesh
FINGER_TIP_MIN_DEPTH = 0.001  # accept a fingertip 1 mm+ inside (no grazes)
FINGER_CAP_DEG = 60.0  # per-finger re-aim rotation cap about the knuckle


def fail(msg):
    print("RIGFORGE_FIT_FAIL:", msg)
    raise SystemExit(msg)


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


def valid_arm_pair(dl, blobs):
    """An outer L/R blob pair big enough to be arms (Section 9).

    Specks (ponytail splits, hair cards, fingertip remnants) read as tiny
    outer blobs; requiring crossings + radius on BOTH sides drops them
    while keeping true arm bands whatever the slice's total blob count.
    """
    r = dl.outer_blob(blobs, 1.0)
    left = dl.outer_blob(blobs, -1.0)
    return (
        r is not None
        and left is not None
        and r["n"] >= MIN_BLOB_N
        and left["n"] >= MIN_BLOB_N
        and r["r"] >= MIN_BLOB_R
        and left["r"] >= MIN_BLOB_R
    )


def robust_arm_bands(dl, top_down, arms):
    """Arm-run bands with a valid outer pair, any blob count."""
    return [i for i in arms if valid_arm_pair(dl, top_down[i]["blobs"])]


def rederive_elbow(dl, top_down, bands, wrist_z):
    """Radius-dip elbow on the robust profile (detector's guards).

    Returns the dip z, or None when the search range is too short. The
    detector's elbow is kept unless this disagrees past
    ELBOW_OVERRIDE_MIN (its band was speck-dragged: the smoothed profile
    dips at the first clean band below the noise).
    """
    in_range = [i for i in bands if top_down[i]["z"] > wrist_z + 0.08]
    if len(in_range) < 3:
        return None
    prof = []
    for i in in_range:
        blobs = top_down[i]["blobs"]
        r = dl.outer_blob(blobs, 1.0)
        left = dl.outer_blob(blobs, -1.0)
        prof.append((r["r"] + left["r"]) / 2)
    band = dl.argmin_interior(dl.smooth(prof), 0, len(in_range))
    return top_down[in_range[band]]["z"]


def track_axes(dl, mesh_path, lm):
    """Envelope remesh + slice/blob analysis; returns arm/leg axis fns."""
    return track_axes_from_subject(dl, dl.import_mesh(mesh_path), lm)


def track_axes_from_subject(dl, subject, lm):
    """track_axes for an in-session subject mesh (no import, no cleanup).

    Same analysis as the path-based entry; the caller owns `subject`
    (typically a joined duplicate) and the returned axes["env"] envelope.
    """
    target = dl.envelope_copy(subject)
    signature, _, _ = dl.slice_signature(target, 120)
    top_down = list(reversed(signature))
    arms = dl.longest_run([i for i, s in enumerate(top_down) if len(s["blobs"]) >= 3])
    legs = dl.longest_run(
        [i for i, s in enumerate(top_down) if len(s["blobs"]) == 2 and i > max(arms)]
    )
    if not arms or not legs:
        fail("no biped signature for axis tracking")
    # Finding 4: arm axis from valid-pair slices above the wrist only —
    # finger splits below the palm are excluded, while true upper-arm
    # bands in count>3 slices (extra blobs from hair/tails) are kept and
    # speck bands (tiny outer blobs) are dropped. Legacy count==3 filter
    # stays as the fallback ladder's second rung.
    robust_all = robust_arm_bands(dl, top_down, arms)
    arm_bands = [i for i in robust_all if top_down[i]["z"] > lm["wrist_z"]]
    band_mode = "robust"
    if len(arm_bands) < MIN_LINE_BANDS:
        arm_bands = [
            i
            for i in arms
            if len(top_down[i]["blobs"]) == 3 and top_down[i]["z"] > lm["wrist_z"]
        ]
        band_mode = "legacy-above-wrist"
    if len(arm_bands) < MIN_LINE_BANDS:
        arm_bands = [i for i in arms if len(top_down[i]["blobs"]) == 3]
        band_mode = "legacy-all"
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
    elbow_new = rederive_elbow(dl, top_down, robust_all, lm["wrist_z"])
    elbow_z = lm["elbow_z"]
    elbow_override = False
    if elbow_new is not None and abs(elbow_new - lm["elbow_z"]) >= ELBOW_OVERRIDE_MIN:
        elbow_z = elbow_new
        elbow_override = True
    # Upper-arm ray: direction of the arm above the elbow (the shoulder
    # back-projects along this, not the forearm direction).
    up_bands = [i for i in arm_bands if top_down[i]["z"] > elbow_z]
    upper_ray = None
    if len(up_bands) >= UPPER_RAY_MIN_BANDS:
        up = fit_line(limb_samples(dl, top_down, up_bands)[0])
        mx, my = up.slope
        upper_ray = Vector((mx, my, 1.0)).normalized()
    return {
        "arm": fit_line(arm_samples),
        "leg": fit_line(leg_samples),
        "arm_bands": len(arm_samples),
        "leg_bands": len(leg_samples),
        "leg_cy": leg_cy,
        "input_asym": round(max(arm_asym, leg_asym), 4),
        "band_mode": band_mode,
        "elbow_z": elbow_z,
        "elbow_lm_z": lm["elbow_z"],
        "elbow_override": elbow_override,
        "upper_ray": upper_ray,
        "upper_ray_bands": len(up_bands),
        "env": target,
        "signature": signature,
        "top_down": top_down,
        "arms_run": arms,
        "legs_run": legs,
        "dl": dl,
    }


def nearest_cy(leg_cy, z):
    if not leg_cy:
        fail("no leg centroid bands for ankle y lookup")
    return min(leg_cy, key=lambda zcy: abs(zcy[0] - z))[1]


def eval_envelope(env):
    """A fresh evaluated envelope for raycasts (scene may have changed)."""
    bpy.context.view_layer.update()
    return env.evaluated_get(bpy.context.evaluated_depsgraph_get())


def mid_state(rv, eval_env, mid, margin):
    """Gate-semantics verdict on a prospective midpoint: (ok, depth)."""
    held = rv.point_depth(eval_env, mid)
    return held["inside"] and held["depth"] >= margin, round(held["depth"], 4)


def mirror_x(v):
    """Mirror of a prospective point (.L solve <-> .R check)."""
    return Vector((-v.x, v.y, v.z))


def nearest_surface_dir(rv, eval_env, mid):
    """Direction of the nearest surface from mid (for nudge steps)."""
    best, best_d = None, None
    for d, (_inside, dist) in zip(rv.DIRS, rv.ray_parity(eval_env, mid), strict=True):
        if dist is not None and (best_d is None or dist < best_d):
            best, best_d = d, dist
    return best


def nudge_target(rv, eval_env, margin, target, mid_of, label, cautions):
    """Bounded verify-nudge: step the target off the nearest surface.

    mid_of(target) lists (bone, midpoint) pairs riding on the target; a
    step is kept only while no passing midpoint un-passes. Returns the
    (possibly moved) target.
    """
    cur = target.copy()

    def states_of(t):
        return [(name, mid, rv.point_depth(eval_env, mid)) for name, mid in mid_of(t)]

    def passed(held):
        return held["inside"] and held["depth"] >= margin

    def snug(held):
        return held["inside"] and held["depth"] >= margin + NUDGE_HEADROOM

    if all(passed(held) for _, _, held in states_of(cur)):
        return cur, 0
    moved = 0
    for _ in range(NUDGE_MAX_STEPS):
        states = states_of(cur)
        if all(snug(held) for _, _, held in states):
            break
        failing = [(name, mid) for name, mid, held in states if not passed(held)]
        pool = failing or [(name, mid) for name, mid, _ in states]
        depths = {name: held["depth"] for name, _, held in states}
        worst_name, worst_mid = min(pool, key=lambda nm: depths[nm[0]])
        step_dir = nearest_surface_dir(rv, eval_env, worst_mid)
        if step_dir is None:
            break
        if worst_name.endswith(".R"):
            # The R midpoint mirrors the L target: convert the desired
            # R-space step back into L-space (x flips, y/z hold).
            step_dir = mirror_x(step_dir)
        trial = cur - step_dir * NUDGE_STEP
        before = {name: passed(held) for name, _, held in states}
        after = {name: passed(held) for name, _, held in states_of(trial)}
        if any(before[name] and not after[name] for name in before):
            break
        cur = trial
        moved += 1
    if moved:
        cautions.append(f"{label} nudged {moved} steps to {[round(v, 4) for v in cur]}")
    return cur, moved


def forefoot_top(env, lm, cy0):
    """Highest forefoot-vertex z (the dorsum the ankle dip must sit near).

    Front = past the ankle band toward the toes; None when too few verts
    (the trigger depending on it then stays silent).
    """
    if lm["facing"] == "-Y":
        front = [
            v.co.z
            for v in env.data.vertices
            if v.co.x > 0.005 and v.co.y < cy0 - 0.05 and v.co.z < lm["ankle_z"]
        ]
    else:
        front = [
            v.co.z
            for v in env.data.vertices
            if v.co.x > 0.005 and v.co.y > cy0 + 0.05 and v.co.z < lm["ankle_z"]
        ]
    return max(front) if len(front) >= 20 else None


def finger_column(dl, axes, lm, wrist_z):
    """Mesh finger-column direction over the palm/finger window.

    Valid-pair bands from below the wrist up into the palm (finger-split
    bands included when the outer pair is intact); returns (unit dir
    pointing down, anchor centroid) or (None, reason).
    """
    top_down = axes["top_down"]
    bands = [
        i
        for i in axes["arms_run"]
        if valid_arm_pair(dl, top_down[i]["blobs"])
        and top_down[i]["z"] < wrist_z + 0.03
        and top_down[i]["z"] > lm["hand_tip_z"] - 0.01
    ]
    if len(bands) < HAND_COL_MIN_BANDS:
        return None, f"only {len(bands)} finger-column bands"
    samples, _ = limb_samples(dl, top_down, bands)
    if len(samples) < HAND_COL_MIN_BANDS:
        return None, "finger column untrackable"
    col = fit_line(samples)
    mx, my = col.slope
    direction = Vector((-mx, -my, -1.0)).normalized()
    anchor = Vector(col((wrist_z + lm["hand_tip_z"]) / 2))
    return (direction, anchor), None


def resolve_targets(lm, axes, preset, rv):
    """Landmark JSON + axis fits -> driver-joint targets + fallback record."""
    if lm["facing"] not in ("-Y", "+Y"):
        fail(f"unsupported facing {lm['facing']!r}")
    elbow_z = axes.get("elbow_z", lm["elbow_z"])
    order = {
        "neck_z": lm["neck_z"],
        "armpit_z": lm["armpit_z"],
        "elbow_z": elbow_z,
        "wrist_z": lm["wrist_z"],
        "hand_tip_z": lm["hand_tip_z"],
        "crotch_z": lm["crotch_z"],
        "knee_z": lm["knee_z"],
    }
    for a, b in (
        ("neck_z", "armpit_z"),
        ("armpit_z", "elbow_z"),
        ("elbow_z", "wrist_z"),
        ("wrist_z", "hand_tip_z"),
        ("crotch_z", "knee_z"),
    ):
        if not order[a] > order[b]:
            fail(f"landmark order violated: {a}={order[a]} <= {b}={order[b]}")
    s = lm["height"] / preset["height"]
    arm, leg = axes["arm"], axes["leg"]
    cautions = []
    eval_env = eval_envelope(axes["env"])
    margin = rv.MARGIN_DEFAULT

    elbow = Vector(arm(elbow_z))
    wrist = Vector(arm(lm["wrist_z"]))
    if axes.get("elbow_override"):
        cautions.append(
            f"elbow re-derived {lm['elbow_z']} -> {round(elbow_z, 4)} "
            "(detector band speck-dragged)"
        )
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

    # Finding 2: back-project the shoulder from the elbow along the
    # upper-arm ray by the preset's upper-arm length; the arm run starts
    # at the armpit, well below the joint. Candidates keep the legacy
    # flat-y solve as one option; the mesh-verified deepest passing
    # placement wins, and a shortening march covers overshoots.
    legacy_up = (elbow - wrist).normalized()
    upper_len = preset["upper_len"] * s
    min_shoulder_z = lm["armpit_z"] + 0.02
    shoulder_off = preset["shoulder_mid_off"] * s

    def shoulder_states(t):
        mid_s, mid_u = t + shoulder_off, (t + elbow) / 2
        return [
            ("shoulder.L", mid_s),
            ("upper_arm.L", mid_u),
            ("shoulder.R", mirror_x(mid_s)),
            ("upper_arm.R", mirror_x(mid_u)),
        ]

    def shoulder_score(t):
        depths = []
        for _, mid in shoulder_states(t):
            ok, depth = mid_state(rv, eval_env, mid, margin)
            if not ok:
                return None
            depths.append(depth)
        return min(depths)

    def clamped(t):
        if t.z < min_shoulder_z and abs(t.z - elbow.z) > 1e-9:
            k = (min_shoulder_z - elbow.z) / (t.z - elbow.z)
            t = elbow + (t - elbow) * k
        return t

    ray = axes.get("upper_ray")
    cands = []
    if ray is not None:
        cands.append(("ray", clamped(elbow + ray * upper_len)))
    legacy = clamped(elbow + legacy_up * upper_len)
    legacy.y = elbow.y
    cands.append(("legacy", legacy))
    scored = [(name, t, shoulder_score(t)) for name, t in cands]
    passing = [(name, t, score) for name, t, score in scored if score is not None]
    shoulder_method = None
    marched = 0
    if passing:
        shoulder_method, shoulder, _ = max(passing, key=lambda ntg: ntg[2])
    else:
        march_dir = ray if ray is not None else legacy_up
        shoulder_method = "march"
        best, best_depth = legacy, -1.0
        frac = 1.0
        while frac >= SHOULDER_MARCH_MIN - 1e-9:
            t = clamped(elbow + march_dir * upper_len * frac)
            score = shoulder_score(t)
            depths = [
                rv.point_depth(eval_env, mid)["depth"] for _, mid in shoulder_states(t)
            ]
            if min(depths) > best_depth:
                best, best_depth = t, min(depths)
            if score is not None:
                best = t
                break
            marched += 1
            frac -= SHOULDER_MARCH_STEP
        shoulder = best
        cautions.append(
            f"shoulder march exhausted ({marched} steps), best-effort used"
            if shoulder_score(shoulder) is None
            else f"shoulder marched to frac {round(frac, 2)}"
        )
    if shoulder.z <= min_shoulder_z + 1e-9 and shoulder_method != "march":
        cautions.append("shoulder clamped above armpit")

    # Finding 3 (ankle): boots hide the joint; fall back to sole offset
    # when the dip is weak, sits implausibly high (mid-boot), or clears
    # the measured forefoot dorsum (a shin narrowing, not the joint).
    cy0 = nearest_cy(axes["leg_cy"], lm["ankle_z"])
    dorsum = forefoot_top(axes["env"], lm, cy0)
    weak = lm["ankle_dip"] < ANKLE_DIP_MIN
    high = lm["ankle_z"] > ANKLE_MAX_Z_FRAC * lm["height"]
    high_foot = dorsum is not None and lm["ankle_z"] > dorsum + FOOT_TOP_MARGIN
    ankle_fb = weak or high or high_foot
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
    toe_vec = preset["toe_vec"] * s
    if lm["facing"] == "+Y":
        foot_vec.y *= -1
        toe_vec.y *= -1
    foot = ankle + foot_vec
    prospective_outside = False
    for mid in ((ankle + foot) / 2, foot + toe_vec / 2):
        if not rv.point_depth(eval_env, mid)["inside"]:
            prospective_outside = True
        if not rv.point_depth(eval_env, mirror_x(mid))["inside"]:
            prospective_outside = True
    if prospective_outside and not ankle_fb:
        ankle_fb = True
        ankle_z = lm["zmin"] + ANKLE_SOLE_FRAC * lm["height"]
        ankle = Vector((leg(ankle_z)[0], nearest_cy(axes["leg_cy"], ankle_z), ankle_z))
        foot = ankle + foot_vec
        cautions.append("ankle forced to sole offset: chain outside the mesh")

    # Hand: the preset vector stands while its midpoint verifies; when it
    # lands outside (merge-shortened hands), the tail is mesh-fit onto
    # the finger column with a span-fit length instead.
    hand = wrist + hand_vec
    hand_fit = False
    hand_mid = (wrist + hand) / 2
    if (
        not rv.point_depth(eval_env, hand_mid)["inside"]
        or not rv.point_depth(eval_env, mirror_x(hand_mid))["inside"]
    ):
        col, reason = finger_column(axes["dl"], axes, lm, wrist.z)
        if col is None:
            cautions.append(f"hand mesh-fit skipped: {reason}")
        else:
            direction, _anchor = col
            span = wrist.z - lm["hand_tip_z"]
            if span < 0.02:
                cautions.append(
                    f"hand mesh-fit skipped: span {round(span, 4)} too short"
                )
            else:
                size = min(hand_vec.length, span * HAND_SPAN_FRAC)
                hand = wrist + direction * size
                hand_fit = True
                cautions.append(
                    f"hand mesh-fit to {[round(v, 4) for v in hand]} "
                    f"(len {round(size, 4)})"
                )

    # Verify-nudge: failing foot/hand midpoints step their tail target
    # off the nearest surface (bounded; never un-passes a passing mid).
    def foot_mids(t):
        mid_f, mid_t = (ankle + t) / 2, t + toe_vec / 2
        return [
            ("foot.L", mid_f),
            ("toe.L", mid_t),
            ("foot.R", mirror_x(mid_f)),
            ("toe.R", mirror_x(mid_t)),
        ]

    def hand_mids(t):
        mid = (wrist + t) / 2
        return [("hand.L", mid), ("hand.R", mirror_x(mid))]

    foot, foot_nudged = nudge_target(
        rv, eval_env, margin, foot, foot_mids, "foot", cautions
    )
    hand, hand_nudged = nudge_target(
        rv, eval_env, margin, hand, hand_mids, "hand", cautions
    )

    targets = {
        "shoulder": shoulder,
        "elbow": elbow,
        "wrist": wrist,
        "hand": hand,
        "hip": hip,
        "knee": knee,
        "ankle": ankle,
        "foot": foot,
    }
    fallbacks = {
        "ankle": {
            "fired": ankle_fb,
            "weak_dip": weak,
            "implausible_z": high,
            "forefoot_trigger": high_foot,
            "dorsum_z": round(dorsum, 4) if dorsum is not None else None,
            "measured_z": lm["ankle_z"],
            "used_z": round(ankle_z, 4),
        },
        "wrist": {
            "fired": wrist_fb,
            "dip": lm["wrist_dip"],
            "used_z": round(wrist.z, 4),
        },
        "elbow": {
            "fired": axes.get("elbow_override", False),
            "measured_z": lm["elbow_z"],
            "used_z": round(elbow_z, 4),
        },
        "shoulder": {
            "method": shoulder_method,
            "marched_steps": marched,
            "ray_bands": axes.get("upper_ray_bands", 0),
        },
        "hand": {
            "mesh_fit": hand_fit,
            "nudged": hand_nudged,
        },
        "foot": {
            "nudged": foot_nudged,
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
    shoulder = bones["shoulder.L"]
    upper = bones["upper_arm.L"]
    preset = {
        "height": height,
        "upper_len": (upper.tail_local - upper.head_local).length,
        "hand_vec": hand.tail_local - hand.head_local,
        "foot_vec": bones["foot.L"].tail_local - bones["foot.L"].head_local,
        "toe_vec": bones["toe.L"].tail_local - bones["toe.L"].head_local,
        "shoulder_mid_off": (shoulder.head_local + shoulder.tail_local) / 2
        - upper.head_local,
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


def mirror_bones(bones, names):
    """Copy solved .L bones onto .R (x negated, roll negated)."""
    for name in names:
        if not name.endswith(".L"):
            fail(f"cannot mirror non-paired bone {name}")
        rname = name[:-2] + ".R"
        if rname not in bones:
            fail(f"missing mirror bone {rname}")
        src, dst = bones[name], bones[rname]
        dst.head = (-src.head.x, src.head.y, src.head.z)
        dst.tail = (-src.tail.x, src.tail.y, src.tail.z)
        dst.roll = -src.roll


FINGER_CHAINS = ("f_index", "f_middle", "f_ring", "f_pinky")


def finger_row_axis(tips):
    """Row axis of the legacy fan: 'x' or 'y', whichever the tips span."""
    xs = [t.x for t in tips.values()]
    ys = [t.y for t in tips.values()]
    return "x" if max(xs) - min(xs) > max(ys) - min(ys) else "y"


def dist_to_seg(p, a, b):
    """Distance from point p to segment a-b (thumb-mask helper)."""
    ab = b - a
    denom = ab.length_squared
    t = (p - a).dot(ab) / denom if denom > 1e-12 else 0.0
    return (p - (a + ab * max(0.0, min(1.0, t)))).length


def mesh_finger_columns(rv, env, lm, tips, thumb_segs):
    """Per-finger mesh columns from a thigh-safe hand slab (+X side).

    The gate's wide slab (|x - wrist_x| < 0.08) catches the thigh when
    the hand rests against it; the medial bound stays tight (-0.03,
    thigh side) while the lateral bound matches the gate (+0.08, splay
    side), and verts near the legacy thumb chains are masked (the thumb
    would own the edge quartile). The slab is quartile-split along the
    fan's row axis
    and each quartile yields a centroid-split dir; the anchor is the
    bottom-quartile-z centroid (below the webbing, on fingertip flesh).
    Quartiles assign to fingers in legacy-tip order along the row.
    The anchor is the lowest flesh vertex + 5 mm (a centroid would land
    between splayed tips); quartiles whose lowest verts are all outside
    flesh fall back to the centroid. Returns {finger: (dir, anchor)}
    with unmeasurable fingers omitted.
    """
    floor = lm["hand_tip_z"] - 0.02
    fingers_roof = (lm["wrist_z"] + lm["hand_tip_z"]) / 2

    def centroid(vs):
        return Vector(
            (
                sum(v.x for v in vs) / len(vs),
                sum(v.y for v in vs) / len(vs),
                sum(v.z for v in vs) / len(vs),
            )
        )

    def column(part):
        zs = sorted(v.z for v in part)
        cut = zs[len(zs) // 2]
        up = [v for v in part if v.z >= cut]
        lo = [v for v in part if v.z < cut]
        if not up or not lo:
            return None
        vec = centroid(lo) - centroid(up)
        if vec.length < 1e-9 or abs(vec.normalized().z) < 0.5:
            return None
        if vec.normalized().z > 0:
            return None
        return vec.normalized(), centroid(lo)

    slab = [
        v.co
        for v in env.data.vertices
        if floor < v.co.z < lm["wrist_z"]
        and -FINGER_SLAB_MEDIAL < v.co.x - lm["wrist_x"] < FINGER_SLAB_LATERAL
        and all(dist_to_seg(v.co, a, b) >= THUMB_MASK_R for a, b in thumb_segs)
    ]
    if len(slab) < FINGER_SLAB_MIN_N:
        return {}, f"only {len(slab)} hand-slab verts"
    # Direction from the full slab (long span averages splay out);
    # anchor from the bottom-z quartile flesh (below the webbing).
    low = [v for v in slab if v.z < fingers_roof]
    axis = finger_row_axis(tips)
    key = (lambda v: v.x) if axis == "x" else (lambda v: v.y)
    fingers = sorted(tips, key=lambda f: key(tips[f]))
    ordered, ordered_low = sorted(slab, key=key), sorted(low, key=key)
    n, n_low = len(ordered), len(ordered_low)
    out = {}
    for rank, finger in enumerate(fingers):
        part = ordered[rank * n // 4 : (rank + 1) * n // 4]
        part_low = ordered_low[rank * n_low // 4 : (rank + 1) * n_low // 4]
        if len(part) < FINGER_SLAB_MIN_N // 4 or not part_low:
            continue
        full = column(part)
        if full is None:
            continue
        direction, _ = full
        eval_env = eval_envelope(env)
        anchor = None
        for v in sorted(part_low, key=lambda v: v.z)[:3]:
            cand = v + Vector((0.0, 0.0, 0.005))
            if rv.point_depth(eval_env, cand)["inside"]:
                anchor = cand
                break
        if anchor is None:
            deep = sorted(part_low, key=lambda v: v.z)[:3]
            base = centroid(deep)
            anchor = base + Vector((0.0, 0.0, 0.005))
        out[finger] = (direction, anchor)
    if not out:
        return {}, "no measurable finger quartile"
    return out, None


def fit_fingers(rv, env, lm, obj, cautions):
    """Re-aim .L finger chains onto the mesh finger columns; mirror to .R.

    Rigid follow keeps the preset fan angle while knuckles sit off the
    mesh; each chain rotates about its knuckle toward its own measured
    mesh column (capped), scales with the shared hand factor, and shifts
    its tip onto its column. Per-finger revert on failed verify (tip
    inside + gate angle under the warn line), so a bad measurement
    leaves the legacy fan in place.
    """
    gate_dir = rv.mesh_finger_dir(env, lm, "L")
    gate_dir_r = rv.mesh_finger_dir(env, lm, "R")
    eval_env = eval_envelope(env)
    mat = obj.matrix_world
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bones = obj.data.edit_bones
    floor = lm["hand_tip_z"] - 0.02
    avail = {}
    for finger in FINGER_CHAINS:
        names = [f"{finger}.{mid}.L" for mid in ("01", "02", "03")]
        chain = [bones.get(n) for n in names]
        if any(b is None for b in chain):
            continue
        if (chain[2].tail - chain[0].head).length < 1e-6:
            continue
        avail[finger] = (names, chain)
    if not avail:
        return []

    def chain_quiet(gdir, head_b, tail_b):
        if gdir is None or head_b is None or tail_b is None:
            return False
        deg = rv.chain_angle(gdir, head_b, tail_b)
        return (
            rv.point_depth(eval_env, mat @ tail_b.tail)["inside"]
            and deg is not None
            and deg < rv.FINGER_WARN_DEG
        )

    if all(
        chain_quiet(gate_dir, chain[0], chain[2])
        and chain_quiet(
            gate_dir_r,
            bones.get(names[0][:-2] + ".R"),
            bones.get(names[2][:-2] + ".R"),
        )
        for names, chain in avail.values()
    ):
        return []
    tips = {f: chain[2].tail.copy() for f, (_, chain) in avail.items()}
    thumb_segs = []
    for mid in ("01", "02", "03"):
        tb = bones.get(f"thumb.{mid}.L")
        if tb is not None:
            thumb_segs.append((tb.head.copy(), tb.tail.copy()))
    cols, reason = mesh_finger_columns(rv, env, lm, tips, thumb_segs)
    if not cols:
        cautions.append(f"finger fit skipped: {reason}")
        return []
    # Per-chain rotate about the knuckle toward its own mesh column; the
    # hand scale below is shared (reference = longest reach), so length
    # ratios survive while each tip lands on its own flesh.
    chains = {}
    saved = {}
    for finger, (names, chain) in avail.items():
        if finger not in cols:
            continue
        mesh_dir, _anchor = cols[finger]
        head_b, tail_b = chain[0], chain[2]
        vec = tail_b.tail - head_b.head
        rot = vec.normalized().rotation_difference(mesh_dir)
        cap = math.radians(FINGER_CAP_DEG)
        if rot.angle > cap:
            rot = Quaternion(rot.axis, cap)
        pivot = head_b.head.copy()
        move = (
            Matrix.Translation(pivot)
            @ rot.to_matrix().to_4x4()
            @ Matrix.Translation(-pivot)
        )
        chains[finger] = (names, chain, pivot)
        saved[finger] = [(b.head.copy(), b.tail.copy()) for b in chain]
        for b in chain:
            b.head = move @ b.head
            b.tail = move @ b.tail
    if not chains:
        return []
    # Per-finger: march down its column to the flesh end, scale the
    # tip just above it (knuckle planted), shift onto the column, then
    # a bounded along-column search for flesh. Placement follows the
    # measured column (verified-inside anchor); the rotation above
    # already aligned the chain. Each step is independent, so one bad
    # measurement cannot wedge the rest.
    for finger, (_, chain, pivot) in chains.items():
        mesh_dir, anchor = cols[finger]
        if rv.point_depth(eval_env, anchor)["inside"]:
            pt, last = anchor, None
            for _ in range(30):
                pt = pt + mesh_dir * 0.0015
                if pt.z < floor:
                    break
                if not rv.point_depth(eval_env, pt)["inside"]:
                    break
                last = pt
            if last is not None:
                target = last - mesh_dir * 0.004
                tip = chain[2].tail.copy()
                if abs(pivot.z - tip.z) > 1e-6:
                    factor = (pivot.z - target.z) / (pivot.z - tip.z)
                    if 0.5 <= factor <= 1.5:
                        for b in chain:
                            b.head = pivot + (b.head - pivot) * factor
                            b.tail = pivot + (b.tail - pivot) * factor
        tip = chain[2].tail.copy()
        if floor < tip.z < lm["wrist_z"]:
            dest = anchor + mesh_dir * ((tip.z - anchor.z) / mesh_dir.z)
            shift = dest - tip
            if shift.length > 0.05:
                shift = shift.normalized() * 0.05
            for b in chain:
                b.head += shift
                b.tail += shift
        held = rv.point_depth(eval_env, mat @ chain[2].tail)
        if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
            continue
        top = min(anchor.z + 0.03, lm["wrist_z"] - 0.01)
        climbed, found = 0, False
        for _ in range(30):
            step = -mesh_dir * 0.0015
            for b in chain:
                b.head += step
                b.tail += step
            climbed += 1
            if chain[2].tail.z > top:
                break
            held = rv.point_depth(eval_env, mat @ chain[2].tail)
            if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
                found = True
                break
        if not found:
            drop = mesh_dir * (0.0015 * climbed)
            for b in chain:
                b.head += drop
                b.tail += drop
        held = rv.point_depth(eval_env, mat @ chain[2].tail)
        if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
            continue
        # Lateral centering for curving caps (the column x/y grazes).
        lateral = [
            (dx, dy)
            for r in (0.001, 0.002, 0.003)
            for dx, dy in (
                (r, 0.0),
                (-r, 0.0),
                (0.0, r),
                (0.0, -r),
                (r, r),
                (r, -r),
                (-r, r),
                (-r, -r),
            )
        ]
        for dx, dy in lateral:
            trial = Vector((dx, dy, 0.0))
            for b in chain:
                b.head += trial
                b.tail += trial
            held = rv.point_depth(eval_env, mat @ chain[2].tail)
            if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
                break
            for b in chain:
                b.head -= trial
                b.tail -= trial
        held = rv.point_depth(eval_env, mat @ chain[2].tail)
        if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
            continue
        for dist in (0.003, 0.006, 0.009, 0.012):
            trial = mesh_dir * dist
            for b in chain:
                b.head += trial
                b.tail += trial
            held = rv.point_depth(eval_env, mat @ chain[2].tail)
            if held["inside"] and held["depth"] >= FINGER_TIP_MIN_DEPTH:
                break
            for b in chain:
                b.head -= trial
                b.tail -= trial
    fixed = []
    for finger, (names, chain, _) in chains.items():
        rnames = [n[:-2] + ".R" for n in names]
        rchain = [bones.get(n) for n in rnames]
        saved_r = [(b.head.copy(), b.tail.copy()) for b in rchain]
        mirror_bones(bones, names)
        head_b, tail_b = chain[0], chain[2]
        deg = rv.chain_angle(gate_dir, head_b, tail_b) if gate_dir else None
        held = rv.point_depth(eval_env, mat @ tail_b.tail)
        deg_r = rv.chain_angle(gate_dir_r, rchain[0], rchain[2]) if gate_dir_r else None
        held_r = rv.point_depth(eval_env, mat @ rchain[2].tail)
        if (
            held["inside"]
            and held["depth"] >= FINGER_TIP_MIN_DEPTH
            and deg is not None
            and deg < rv.FINGER_WARN_DEG
            and held_r["inside"]
            and held_r["depth"] >= FINGER_TIP_MIN_DEPTH
            and deg_r is not None
            and deg_r < rv.FINGER_WARN_DEG
        ):
            fixed.append(finger)
        else:
            for b, (h, t) in zip(chain, saved[finger], strict=True):
                b.head, b.tail = h, t
            for b, (h, t) in zip(rchain, saved_r, strict=True):
                b.head, b.tail = h, t
    if fixed:
        cautions.append(f"fingers re-aimed: {','.join(fixed)}")
    return fixed


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


def emit_module(obj, out_path, mesh_path, lm_path, preset="hll_hero"):
    from rigforge.utils.rig import write_metarig

    code = write_metarig(
        obj, layers=True, func_name="create", groups=True, widgets=True
    )
    header = (
        "# SPDX-License-Identifier: GPL-2.0-or-later\n"
        f"# Fitted metarig, generated by tools/fit_metarig.py from {preset}\n"
        f"# Subject: {os.path.basename(mesh_path)} | landmarks: "
        f"{os.path.basename(lm_path)} — do not hand-edit, re-fit instead.\n\n"
    )
    compile(header + code, out_path, "exec")  # emitted module must parse
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + code + "\n")
    return out_path


def check_closure(obj, drivers=DRIVERS, extra=("spine.004", "face")):
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
        elif b.name in drivers or b.name in extra:
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


def fit_hero(hp, align_roll, obj, lm, axes, rv):
    """Solve the hll_hero metarig `obj` onto landmarks; returns fit record.

    Call in OBJECT mode with `obj` active (switches to EDIT internally,
    like fit_stalker). Mutates `obj` in place; ends in EDIT mode. Shared
    by the headless CLI and wm.rigforge_auto_place.
    """
    preset, snap = snapshot_preset(hp, obj)
    targets, fallbacks, cautions, s = resolve_targets(lm, axes, preset, rv)
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
            hp, align_roll, obj, snap, bone, targets[head_k], targets[tail_k]
        )
    realigned_bones = [b for b in angles if angles[b] > ROLL_RECOMPUTE_DEG]
    mirror_subtree(hp, bones, "shoulder.L")
    mirror_subtree(hp, bones, "thigh.L")
    hp.reconnect_from(obj, "rigforge_hll_hero_metarig_add")
    fingers_fixed = fit_fingers(rv, axes["env"], lm, obj, cautions)

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
    return {
        "scale": s,
        "fallbacks": fallbacks,
        "cautions": cautions,
        "fingers_fixed": fingers_fixed,
        "neck": {"dz": neck_dz, "stretch": neck_k, "torso_k": torso_k},
        "joints": joints,
        "length_change_pct": lengths,
        "roll_realigned": sorted(realigned_bones),
        "closure": {"max_gap": max_gap, "open_drivers": open_drivers},
        "symmetry_max": symmetry,
    }


STALKER_SPINE = (
    "spine.001",
    "spine.002",
    "spine.003",
    "spine.004",
    "spine.005",
    "spine.006",
)
STALKER_NECK = ("neck.001", "neck.002", "neck.003", "neck.004", "head")
STALKER_FRONT_LEG = (
    "upper_arm.L",
    "forearm.L",
    "forefoot.L",
    "f_toe.L",
    "f_hoof.L",
)
STALKER_REAR_LEG = (
    "thigh.L",
    "lower_leg.L",
    "hind_foot.L",
    "r_toe.L",
    "r_hoof.L",
)
STALKER_TAIL = ("tail.001", "tail.002", "tail.003", "tail.004", "tail.005")
STALKER_DRIVERS = (
    "upper_arm.L",
    "forearm.L",
    "forefoot.L",
    "thigh.L",
    "lower_leg.L",
    "hind_foot.L",
)
STALKER_CLOSURE_DRIVERS = (
    *STALKER_SPINE,
    *STALKER_NECK,
    "shoulder.L",
    "shoulder.R",
    "pelvis.L",
    "pelvis.R",
    "upper_arm.L",
    "upper_arm.R",
    "forearm.L",
    "forearm.R",
    "forefoot.L",
    "forefoot.R",
    "thigh.L",
    "thigh.R",
    "lower_leg.L",
    "lower_leg.R",
    "hind_foot.L",
    "hind_foot.R",
    *STALKER_TAIL,
)


def stalker_targets(lm):
    """Quadruped landmark JSON -> driver-joint targets (all measured 3D)."""
    if lm.get("kind") != "quadruped":
        fail(f"stalker fit needs kind=quadruped landmarks, got {lm.get('kind')!r}")
    if lm["facing"] not in ("-Y", "+Y"):
        fail(f"unsupported facing {lm['facing']!r}")
    for key in (
        "spine_front",
        "spine_rear",
        "neck_base",
        "head",
        "skull",
        "tail_base",
        "legs",
    ):
        if lm.get(key) is None:
            fail(f"landmark JSON missing {key}")
    for leg in ("FL", "BL"):
        if leg not in lm["legs"]:
            fail(f"landmark JSON legs missing {leg} (need FL/BL, .R mirrors)")
        for joint in ("top", "mid", "foot", "toe"):
            if lm["legs"][leg].get(joint) is None:
                fail(f"landmark JSON legs.{leg} missing {joint}")

    def v(p):
        return Vector(p)

    return {
        "spine_rear": v(lm["spine_rear"]),
        "spine_front": v(lm["spine_front"]),
        "neck_base": v(lm["neck_base"]),
        "head": v(lm["head"]),
        "skull": v(lm["skull"]),
        "tail_base": v(lm["tail_base"]),
        "tail_tip": v(lm["tail_tip"]) if lm.get("tail_tip") is not None else None,
        "tail_fallback": bool(lm.get("tail_base_fallback")),
        "front_top": v(lm["legs"]["FL"]["top"]),
        "front_mid": v(lm["legs"]["FL"]["mid"]),
        "front_foot": v(lm["legs"]["FL"]["foot"]),
        "front_toe": v(lm["legs"]["FL"]["toe"]),
        "rear_top": v(lm["legs"]["BL"]["top"]),
        "rear_mid": v(lm["legs"]["BL"]["mid"]),
        "rear_foot": v(lm["legs"]["BL"]["foot"]),
        "rear_toe": v(lm["legs"]["BL"]["toe"]),
    }


def chain_fractions(bones, chain):
    """Preset length fraction per bone over the chain's total length."""
    lens = [(bones[n].tail - bones[n].head).length for n in chain]
    total = sum(lens)
    if total <= 1e-9:
        fail(f"chain {[n for n in chain]} collapsed")
    return [L / total for L in lens]


def solve_chain(hp, obj, snap, chain, points):
    """Lay a connected chain onto target points (root outward).

    Each bone's head lands on its point, tail on the next; children
    rigid-follow; rolls preserved unless the direction changed past
    ROLL_RECOMPUTE_DEG (same rule as solve_bone). Returns {bone: deg}.
    """
    from rigforge.utils.bones import align_bone_z_axis

    bones = obj.data.edit_bones
    angles = {}
    # points carries N+1 entries for N bones by design (shared joints).
    for name, head_t, tail_t in zip(chain, points, points[1:], strict=False):
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
        deg = math.degrees(snap[name]["dir"].angle(new_dir))
        if deg > ROLL_RECOMPUTE_DEG:
            align_bone_z_axis(obj, name, snap[name]["z_axis"])
        angles[name] = deg
    return angles


def _renorm(fracs):
    total = sum(fracs)
    return [f / total for f in fracs]


def lerp_points(start, end, fracs):
    """Chain points from start to end splitting by preset fractions."""
    pts, acc = [start], 0.0
    for f in fracs[:-1]:
        acc += f
        pts.append(start + (end - start) * acc)
    pts.append(end)
    return pts


def snapshot_stalker_preset(hp, obj):
    """Preset measures in OBJECT mode + per-bone dirs for roll checks."""
    bones = obj.data.bones
    head = bones["spine.001"].head_local
    tail = bones["spine.006"].tail_local
    preset = {"spine_len": (tail - head).length}
    snap = {}
    for name in (
        *STALKER_SPINE,
        *STALKER_NECK,
        *STALKER_FRONT_LEG,
        *STALKER_REAR_LEG,
        *STALKER_TAIL,
    ):
        b = bones[name]
        snap[name] = {
            "head": b.head_local.copy(),
            "tail": b.tail_local.copy(),
            "dir": (b.tail_local - b.head_local).normalized(),
            "z_axis": b.matrix_local.col[2].to_3d().normalized(),
        }
    return preset, snap


def stalker_joint_positions(bones, edit):
    h, t = ("head", "tail") if edit else ("head_local", "tail_local")

    def at(bone, which):
        return getattr(bones[bone], which)

    return {
        "spine_rear": at("spine.001", h),
        "spine_front": at("spine.006", t),
        # neck_base lands mid-chain (neck.002.tail); neck.001.head plants
        # on the chest top (spine_front) by the connected-chain design.
        "neck_base": at("neck.002", t),
        "head": at("head", h),
        "skull": at("head", t),
        "tail_base": at("tail.001", h),
        "front_top": at("upper_arm.L", h),
        "front_mid": at("upper_arm.L", t),
        "front_foot": at("forearm.L", t),
        "front_toe": at("forefoot.L", t),
        "rear_top": at("thigh.L", h),
        "rear_mid": at("thigh.L", t),
        "rear_foot": at("lower_leg.L", t),
        "rear_toe": at("hind_foot.L", t),
    }


SPINE_END_NEED = 0.015  # inset march: endpoint depth target (gate + headroom)
SPINE_END_MAX = 0.05  # inset march bound
SPINE_END_STEP = 0.01
TOE_SPAN_MIN = 0.01  # below: stub too small to aim, rigid-follow instead
TAIL_SPAN_MIN = 0.03  # below: rigid-follow the tail instead of solving


def _deep_enough(rv, eval_env, pt, need):
    held = rv.point_depth(eval_env, pt)
    return held["inside"] and held["depth"] >= need


def inset_spine_end(rv, env, point, inward, cautions, label):
    """March a spine endpoint inward until it sits 15 mm deep (bounded).

    End-slice centroids sit on the body skin (the chest top / pelvis
    belong inside the ribcage, not at the surface). Verify-then-correct:
    untouched when already deep, bounded march otherwise, measured kept
    + caution when the march exhausts.
    """
    eval_env = eval_envelope(env)
    cur, moved = point.copy(), 0.0
    while moved < SPINE_END_MAX:
        if _deep_enough(rv, eval_env, cur, SPINE_END_NEED):
            break
        cur += inward * SPINE_END_STEP
        moved += SPINE_END_STEP
    if not _deep_enough(rv, eval_env, cur, SPINE_END_NEED):
        cautions.append(f"{label} inset exhausted, measured kept")
        return point
    if moved:
        cautions.append(f"{label} inset {moved * 100:.0f} cm for depth")
    return cur


def inset_chest_top(rv, env, front, neck_base, frac0, inward, cautions):
    """March the chest top back until the neck.001 midpoint passes.

    The neck chain bends out of the chest toward the neck, so the chest
    top's own depth does not predict the first neck bone's: the
    prospective midpoint (pure target arithmetic, no solve needed) is
    the verified quantity. Without this the straight chain grazes the
    concave throatlatch (synthetic neck.001 read 4.8 mm). Same
    verify-then-correct contract as inset_spine_end.
    """
    eval_env = eval_envelope(env)
    # Inward runs along the spine (rearward): the chest top retreats
    # into the ribcage while the neck via holds.
    cur, moved = front.copy(), 0.0
    while moved < SPINE_END_MAX:
        seg = neck_base - cur
        mid = cur + seg * (frac0 / 2)
        if _deep_enough(rv, eval_env, cur, SPINE_END_NEED) and _deep_enough(
            rv, eval_env, mid, rv.MARGIN_DEFAULT + 0.002
        ):
            break
        cur += inward * SPINE_END_STEP
        moved += SPINE_END_STEP
    seg = neck_base - cur
    mid = cur + seg * (frac0 / 2)
    if not _deep_enough(rv, eval_env, mid, rv.MARGIN_DEFAULT + 0.002):
        cautions.append("spine_front inset exhausted, measured kept")
        return front
    if moved:
        cautions.append(f"spine_front inset {moved * 100:.0f} cm for depth")
    return cur


def fit_stalker(hp, obj, targets, s, snap, cautions):
    """Root-outward stalker solve onto measured targets. Returns angles.

    Call in OBJECT mode (switches to EDIT internally, like the hero
    path's solve block). Mirrors .L to .R and restores stock connect
    flags; closure/symmetry verification stays with the caller.
    """
    bpy.ops.object.mode_set(mode="EDIT")
    bones = obj.data.edit_bones
    for b in bones:  # as in make_hll_presets: no connected dragging
        b.use_connect = False
    hp.apply(bones, [b.name for b in bones], Matrix.Scale(s, 4))  # coarse fit

    angles = {}
    # Spine: connected chain along the measured rear->front line.
    fracs = chain_fractions(bones, STALKER_SPINE)
    angles.update(
        solve_chain(
            hp,
            obj,
            snap,
            STALKER_SPINE,
            lerp_points(targets["spine_rear"], targets["spine_front"], fracs),
        )
    )
    # Neck/head: ONE connected rigid chain (the head rig requires it —
    # neck.001..head stay joint-closed, placed as a unit, never detached
    # per-bone). neck.001.head plants on the chest top; the chain runs
    # spine_front -> neck_base -> head -> skull in two segments plus the
    # head bone, so every measured head landmark is honored.
    nfracs = chain_fractions(bones, STALKER_NECK[:4])
    seg1 = lerp_points(
        targets["spine_front"], targets["neck_base"], _renorm(nfracs[:2])
    )
    seg2 = lerp_points(targets["neck_base"], targets["head"], _renorm(nfracs[2:]))
    neck_pts = [seg1[0], seg1[1], seg1[2], seg2[1], seg2[2], targets["skull"]]
    angles.update(solve_chain(hp, obj, snap, STALKER_NECK, neck_pts))
    # Legs: root subtree shift onto the measured top, then outward solve;
    # toe/hoof rigid-follow (blockout feet are featureless stubs).
    for root, chain, keys in (
        (
            "shoulder.L",
            STALKER_FRONT_LEG[:3],
            ("front_top", "front_mid", "front_foot", "front_toe"),
        ),
        (
            "pelvis.L",
            STALKER_REAR_LEG[:3],
            ("rear_top", "rear_mid", "rear_foot", "rear_toe"),
        ),
    ):
        hp.apply(
            bones,
            hp.descendants(bones, root),
            Matrix.Translation(targets[keys[0]] - bones[chain[0]].head),
        )
        angles.update(solve_chain(hp, obj, snap, chain, [targets[k] for k in keys]))
    # Toes: aim the stub chain from the toe landmark back at the foot
    # center (blockout feet have no toe sub-structure; the preset
    # horse-length chain cannot stand on them). Rigid follow stays for
    # degenerate spans only.
    for chain, foot_k, toe_k in (
        (("f_toe.L", "f_hoof.L"), "front_foot", "front_toe"),
        (("r_toe.L", "r_hoof.L"), "rear_foot", "rear_toe"),
    ):
        span = targets[foot_k] - targets[toe_k]
        if span.length < TOE_SPAN_MIN:
            cautions.append(f"{chain[0]} span tiny, rigid follow")
            continue
        fracs = _renorm(chain_fractions(bones, chain))
        pts = lerp_points(targets[toe_k], targets[foot_k], fracs)
        angles.update(solve_chain(hp, obj, snap, chain, pts))
    # Tail: solved base->tip when measured (the preset tail otherwise
    # overshoots small cones); rigid follow on fallback/short spans.
    tail_tip = targets.get("tail_tip")
    tail_span = (
        (tail_tip - targets["tail_base"]).length if tail_tip is not None else 0.0
    )
    if tail_span >= TAIL_SPAN_MIN:
        fracs = chain_fractions(bones, STALKER_TAIL)
        angles.update(
            solve_chain(
                hp,
                obj,
                snap,
                STALKER_TAIL,
                lerp_points(targets["tail_base"], tail_tip, fracs),
            )
        )
    else:
        hp.apply(
            bones,
            hp.descendants(bones, "tail.001"),
            Matrix.Translation(targets["tail_base"] - bones["tail.001"].head),
        )
        if not targets["tail_fallback"]:
            cautions.append("tail span short, rigid follow")
    # Midline bones (spine/neck/head/tail) need no mirror; the leg-root
    # subtrees are pure .L (verified in the stalker preset), plus the
    # breast plate hanging off the spine.
    mirror_subtree(hp, bones, "shoulder.L")
    mirror_subtree(hp, bones, "pelvis.L")
    mirror_bones(bones, ["breast.L"])
    hp.reconnect_from(obj, "rigforge_hll_stalker_metarig_add")
    return angles


def run_generate(obj):
    """In-session headless generate; returns the generate proof dict."""
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = obj
    bpy.ops.pose.rigforge_generate()
    rig = next(
        (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data),
        None,
    )
    if rig is None:
        fail("generate produced no rig")
    n_def = sum(1 for b in rig.data.bones if b.name.startswith("DEF-"))
    print(f"generate: rig bones={len(rig.data.bones)} def={n_def}")
    if n_def <= 30:
        fail(f"only {n_def} DEF bones")
    print("RIGFORGE_FIT_GENERATE_OK")
    return {"rig_bones": len(rig.data.bones), "def_bones": n_def}


def hint_target_overrides(uh, lm, hints_path, rotated):
    """Arbitrate a unirig-hints doc against landmarks; CLI + operator share.

    Returns (hints_report, overrides): overrides maps target roles to
    trusted Vector positions; the caller applies the roles it knows.
    """
    report = uh.arbitrate(uh.load_hints(hints_path), lm, rotated)
    overrides = {role: Vector(pos) for role, pos in report["trusted"].items()}
    return report, overrides


def fit_stalker_object(hp, rv, obj, lm, env, targets, cautions):
    """Solve the hll_stalker metarig `obj` onto targets; returns fit record.

    Call in OBJECT mode with `obj` active. `targets` is the (possibly
    hint-overridden) stalker_targets dict; spine insets mutate it in
    place. `env` verifies the inset marches. Mutates `obj` in place;
    ends in EDIT mode. Shared by the headless CLI and
    wm.rigforge_auto_place.
    """
    preset, snap = snapshot_stalker_preset(hp, obj)
    spine_axis = targets["spine_front"] - targets["spine_rear"]
    if spine_axis.length < 1e-9:
        fail("spine endpoints coincide")
    spine_axis.normalize()
    neck_frac0 = (snap["neck.001"]["tail"] - snap["neck.001"]["head"]).length / (
        (snap["neck.001"]["tail"] - snap["neck.001"]["head"]).length
        + (snap["neck.002"]["tail"] - snap["neck.002"]["head"]).length
    )
    targets["spine_front"] = inset_chest_top(
        rv,
        env,
        targets["spine_front"],
        targets["neck_base"],
        neck_frac0,
        -spine_axis,
        cautions,
    )
    targets["spine_rear"] = inset_spine_end(
        rv, env, targets["spine_rear"], spine_axis, cautions, "spine_rear"
    )

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.objects.active = obj
    s = (targets["spine_front"] - targets["spine_rear"]).length / preset["spine_len"]
    before = {
        k: v * s for k, v in stalker_joint_positions(obj.data.bones, False).items()
    }
    if targets["tail_fallback"]:
        cautions.append("tail_base fallback: no tail geometry, rigid follow")

    angles = fit_stalker(hp, obj, targets, s, snap, cautions)
    realigned_bones = [b for b in angles if angles[b] > ROLL_RECOMPUTE_DEG]
    max_gap, open_drivers = check_closure(
        obj, drivers=STALKER_CLOSURE_DRIVERS, extra=()
    )
    bones = obj.data.edit_bones  # refetch: reconnect_from switched modes
    symmetry = check_symmetry(bones)
    after = stalker_joint_positions(bones, True)
    joints = {}
    for key, tgt in targets.items():
        if key in ("tail_fallback", "tail_tip"):
            continue
        joints[key] = {
            "target": [round(v, 4) for v in tgt],
            "before_d": round((before[key] - tgt).length, 4),
            "after_d": round((after[key] - tgt).length, 4),
        }
    lengths = {}
    for bone in STALKER_DRIVERS:
        old = (snap[bone]["tail"] - snap[bone]["head"]).length * s
        new = (bones[bone].tail - bones[bone].head).length
        lengths[bone] = round(100 * (new - old) / old, 1)
    return {
        "scale": s,
        "joints": joints,
        "length_change_pct": lengths,
        "roll_realigned": sorted(realigned_bones),
        "closure": {"max_gap": max_gap, "open_drivers": open_drivers},
        "symmetry_max": symmetry,
    }


def main_stalker(args, mesh_path, lm_path, lm, out_path, blend_path, want_generate):
    """Stalker fit flow: measured 3D targets, connected-chain solve."""
    dl, hp, uh, rv = _detect, _presets, _hints, _validate
    try:
        bpy.ops.preferences.addon_enable(module="rigforge")
    except RuntimeError:
        fail("rigforge addon not found; set BLENDER_USER_SCRIPTS overlay")
    import rigforge

    print("rigforge:", rigforge.__file__)

    targets = stalker_targets(lm)
    hints_report = None
    if "--hints" in args:
        hints_report, overrides = hint_target_overrides(
            uh, lm, args[args.index("--hints") + 1], "--hints-rotated" in args
        )
        for role, pos in overrides.items():
            if role in targets:
                targets[role] = pos
        n_trust = len(hints_report["trusted"])
        n_sup = len(hints_report["supporting"])
        n_div = len(hints_report["diverged"])
        print(f"hints: {n_trust} trusted, {n_sup} supporting, {n_div} measurement-wins")

    # Session side effect (same as the hero's track_axes): joined
    # subject + envelope, so landmark coordinates apply directly and
    # downstream stages (gate, auto_rig bind) find their objects.
    subject = dl.import_mesh(mesh_path)
    env = dl.envelope_copy(subject)

    cautions = []
    # Chest top first (its march verifies the neck.001 midpoint, and
    # needs the preset neck fraction — measured pre-solve in OBJECT
    # mode via a throwaway metarig-free length ratio is overkill; the
    # fraction comes from the stock preset proportions below).
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.rigforge_hll_stalker_metarig_add()
    obj = bpy.context.view_layer.objects.active
    fit = fit_stalker_object(hp, rv, obj, lm, env, targets, cautions)

    emit_module(obj, out_path, mesh_path, lm_path, preset="hll_stalker")
    generate = run_generate(obj) if want_generate else None
    if blend_path:
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    report = {
        "preset": "hll_stalker",
        "subject": mesh_path,
        "facing": lm["facing"],
        "length": lm["length"],
        "scale": round(fit["scale"], 4),
        "cautions": cautions,
        "hints": hints_report,
        "joints": fit["joints"],
        "length_change_pct": fit["length_change_pct"],
        "roll_realigned": fit["roll_realigned"],
        "closure": {
            "max_gap": round(fit["closure"]["max_gap"], 6),
            "open_drivers": fit["closure"]["open_drivers"],
        },
        "symmetry_max": round(fit["symmetry_max"], 6),
        "emit": out_path,
        "generate": generate,
    }
    print("RIGFORGE_FIT_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_FIT_OK")


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) < 2 or args[0].startswith("-"):
        fail("usage: fit_metarig.py MESH LANDMARKS.json [--out F] [--blend F]")
    mesh_path, lm_path = args[0], args[1]
    with open(lm_path, encoding="utf-8") as f:
        lm = json.load(f)
    kind = lm.get("kind", "biped")
    preset = (
        args[args.index("--preset") + 1]
        if "--preset" in args
        else {"biped": "hll_hero", "quadruped": "hll_stalker"}.get(kind)
    )
    if preset not in ("hll_hero", "hll_stalker"):
        fail(f"--preset must be hll_hero or hll_stalker, got {preset!r}")
    if (preset == "hll_stalker") != (kind == "quadruped"):
        fail(f"preset {preset} needs kind={kind!r} landmarks")
    tag = "stalker" if preset == "hll_stalker" else "hero"
    out_path = (
        args[args.index("--out") + 1]
        if "--out" in args
        else f"/tmp/rigforge_fitted_{tag}.py"
    )
    blend_path = (
        args[args.index("--blend") + 1]
        if "--blend" in args
        else f"/tmp/rigforge_fitted_{tag}.blend"
    )
    want_generate = "--no-generate" not in args
    if "--no-blend" in args:
        blend_path = None

    if preset == "hll_stalker":
        main_stalker(args, mesh_path, lm_path, lm, out_path, blend_path, want_generate)
        return

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

    dl, hp, rv = _detect, _presets, _validate
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
    fit = fit_hero(hp, align_bone_z_axis, obj, lm, axes, rv)

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
        "scale": round(fit["scale"], 4),
        "axis_bands": {"arm": axes["arm_bands"], "leg": axes["leg_bands"]},
        "band_mode": axes["band_mode"],
        "input_asym": axes["input_asym"],
        "fallbacks": fit["fallbacks"],
        "cautions": fit["cautions"],
        "fingers_fixed": fit["fingers_fixed"],
        "neck": {
            "dz": round(fit["neck"]["dz"], 4),
            "stretch": round(fit["neck"]["stretch"], 3),
            "torso_k": round(fit["neck"]["torso_k"], 3),
        },
        "joints": fit["joints"],
        "length_change_pct": fit["length_change_pct"],
        "roll_realigned": fit["roll_realigned"],
        "closure": {
            "max_gap": round(fit["closure"]["max_gap"], 6),
            "open_drivers": fit["closure"]["open_drivers"],
        },
        "symmetry_max": round(fit["symmetry_max"], 6),
        "emit": out_path,
        "generate": generate,
    }
    print("RIGFORGE_FIT_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_FIT_OK")


if __name__ == "__main__":
    main()
