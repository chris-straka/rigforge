# SPDX-License-Identifier: GPL-2.0-or-later
"""Validation gate for a fitted metarig (auto-placement Phase 3).

Headless (from the repo root; the preset fallback needs the addon overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/validate_fit.py -- \\
      MESH FITTED_METARIG.py LANDMARKS.json [--margin M] [--render DIR]
      [--no-render] [--on-fail stop|preset] [--open bone,...] [--report PATH]

A fitted metarig must pass this gate before it is allowed near `generate`
(docs/auto_placement.md Section 4.4). Hard gates (failures stop the
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

- fingers: each four-finger chain direction vs the mesh finger direction
  (centroid split of sub-wrist verts); warns past 45 deg, i.e. the bone
  points more sideways than along the mesh fingers (the preset's natural
  fan reads ~38 deg on the outer fingers). Thumb excluded (it points
  sideways by anatomy).
- toes: toe-tip overshoot past the mesh foot front in the facing axis.
- head: head-top vs a skullcap estimate (widest head-blob ring + its
  radius), NOT mesh zmax — hair spikes must not count.

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
TOE_WARN = 0.002  # toe-tip overshoot past the mesh foot front
HEAD_WARN = 0.03  # head-top vs skullcap estimate
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
    ns["create"](obj)
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def check_closure(meta):
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
        elif b.name in DRIVER_BONES:
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


def check_inside(meta, eval_env, margin):
    """Per-driver-bone surface stats; midpoint must be inside + margin."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    stats, failures = {}, []
    for name in DRIVER_BONES:
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


def mesh_finger_dir(env, lm, side):
    """Mesh finger direction: centroid split of sub-wrist verts, else None."""
    sx = lm["wrist_x"] if side == "L" else -lm["wrist_x"]
    verts = [
        v.co
        for v in env.data.vertices
        if v.co.z < lm["wrist_z"] and abs(v.co.x - sx) < 0.08
    ]
    if len(verts) < 50:
        return None
    zs = sorted(v.z for v in verts)
    cut = zs[len(zs) // 2]
    up = [v for v in verts if v.z >= cut]
    lo = [v for v in verts if v.z < cut]
    if not up or not lo:
        return None

    def centroid(vs):
        n = len(vs)
        return Vector(
            (
                sum(v.x for v in vs) / n,
                sum(v.y for v in vs) / n,
                sum(v.z for v in vs) / n,
            )
        )

    vec = centroid(lo) - centroid(up)
    return vec.normalized() if vec.length > 1e-9 else None


def report_fingers(meta, env, lm):
    """Warn-only: finger-chain orientation vs mesh finger direction."""
    bones = meta.data.edit_bones
    out = {}
    for side in SIDES:
        mesh_dir = mesh_finger_dir(env, lm, side)
        if mesh_dir is None:
            out[side] = {"status": "no-data", "reason": "too few sub-wrist verts"}
            continue
        worst, worst_finger, angles = 0.0, None, {}
        for finger in FINGERS:
            head_b = bones.get(f"{finger}.01.{side}")
            tail_b = bones.get(f"{finger}.03.{side}")
            if head_b is None or tail_b is None:
                continue
            vec = tail_b.tail - head_b.head
            if vec.length < 1e-9:
                continue
            deg = math.degrees(mesh_dir.angle(vec.normalized()))
            angles[finger] = round(deg, 1)
            if deg > worst:
                worst, worst_finger = deg, finger
        if worst_finger is None:
            out[side] = {"status": "no-data", "reason": "finger bones missing"}
            continue
        out[side] = {
            "status": "warn" if worst > FINGER_WARN_DEG else "ok",
            "mesh_dir": [round(v, 3) for v in mesh_dir],
            "angles_deg": angles,
            "worst": worst_finger,
            "worst_deg": round(worst, 1),
        }
    return out


def report_toes(meta, env, eval_env, lm):
    """Warn-only: toe-tip overshoot past the mesh foot front."""
    bones = meta.data.edit_bones
    mat = meta.matrix_world
    if lm["facing"] not in ("-Y", "+Y"):
        return {"status": "no-data", "reason": f"facing {lm['facing']!r}"}
    foot = [v.co for v in env.data.vertices if v.co.z < lm["ankle_z"] + 0.01]
    if not foot:
        return {"status": "no-data", "reason": "no sub-ankle verts"}
    front = min(v.y for v in foot) if lm["facing"] == "-Y" else max(v.y for v in foot)
    out = {"foot_front": round(front, 4)}
    for side in SIDES:
        b = bones.get(f"toe.{side}")
        if b is None:
            out[side] = {"status": "no-data", "reason": "bone missing"}
            continue
        tip = mat @ b.tail
        over = front - tip.y if lm["facing"] == "-Y" else tip.y - front
        tip_pt = point_depth(eval_env, tip)
        out[side] = {
            "status": "warn" if over > TOE_WARN else "ok",
            "tip": [round(v, 4) for v in tip],
            "overshoot": round(over, 4),
            "tip_inside": tip_pt["inside"],
        }
    return out


def skullcap_z(signature, lm):
    """Skullcap from head-blob width: widest ring above the neck dip + r."""
    top_down = list(reversed(signature))
    bands = [(i, s) for i, s in enumerate(top_down) if len(s["blobs"]) == 1]
    bands = [(i, s) for i, s in bands if s["z"] > lm["neck_z"]]
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

    closure = check_closure(meta)
    gaps = {k: round(v, 6) for k, v in closure["gaps"].items()}
    unlisted = [b for b in closure["open_drivers"] if b not in open_allow]
    closure_ok = not gaps and not unlisted

    stats, inside_fail = check_inside(meta, eval_env, margin)
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
    report = {
        "margin": margin,
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
        "warns": {
            "fingers": report_fingers(meta, env, lm),
            "toes": report_toes(meta, env, eval_env, lm),
            "head": report_head(meta, signature, lm),
        },
        "overlay": None,
        "verdict": "pass" if not failures else "fail",
        "failures": failures,
    }
    bpy.ops.object.mode_set(mode="OBJECT")
    return report


def render_overlays(env, meta, lm, out_dir):
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
        for name in DRIVER_BONES:
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
    for key in ("facing", "height", "zmax", "neck_z", "wrist_z", "wrist_x", "ankle_z"):
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
    report = run_gate(env, meta, lm, signature, margin, open_allow)
    report["subject"] = mesh_path
    report["metarig"] = meta_path
    if want_render:
        report["overlay"] = render_overlays(env, meta, lm, render_dir)

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
    bpy.ops.object.rigforge_hll_hero_metarig_add()
    preset = bpy.context.view_layer.objects.active
    fallback = run_gate(env, preset, lm, signature, margin, open_allow)
    fallback["subject"] = mesh_path
    fallback["metarig"] = "stock hll_hero preset as-is"
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
