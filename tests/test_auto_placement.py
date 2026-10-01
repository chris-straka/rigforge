# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: auto-placement fit + gate + generate + export (Phase 3).

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_auto_placement.py

End to end on the synthetic A-pose biped (no external assets): build mesh,
detect landmarks, fit the hll_hero metarig, run the validation gate
(in-session and CLI), generate, game-export, and prove the fit-delta
numbers. Also proves the failure path (a failed gate stops the pipeline
naming bones) and the preset-as-is fallback rung.

Section 8 adds the production regression subjects (Andras + two Hunyuan
meshes, read-only external files): landmarks, fit with generate, and the
in-session gate must pass on each. Subjects whose files are absent are
skipped (external assets may rot); present subjects must pass.

Pass criteria: exits 0 and prints RIGFORGE_AUTOPLACE_OK.
"""

import contextlib
import importlib.util
import io
import json
import os
import runpy
import shutil
import struct
import sys

import bpy

TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"
)
TMP = "/tmp/rigforge_ap_test"
# Driver joints the hll_hero stock preset floats by design (socket/hip/neck
# pivots with real gaps) — explicitly listed as intentionally open.
OPEN = (
    "shoulder.L",
    "shoulder.R",
    "spine.004",
    "thigh.L",
    "thigh.R",
    "upper_arm.L",
    "upper_arm.R",
)


def fail(msg):
    print("RIGFORGE_AUTOPLACE_FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def load_tool(name, filename):
    path = os.path.join(TOOLS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_tool(filename, argv):
    """Run a tools/ script in-session with patched argv; capture stdout."""
    old_argv = sys.argv
    buf = io.StringIO()
    code = None
    sys.argv = ["tool", "--", *argv]
    try:
        with contextlib.redirect_stdout(buf):
            runpy.run_path(os.path.join(TOOLS, filename), run_name="__main__")
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old_argv
    return buf.getvalue(), code


def parse_report(output, begin, end):
    try:
        payload = output.split(begin)[1].split(end)[0]
    except IndexError:
        fail(f"markers {begin}/{end} missing from tool output")
    return json.loads(payload)


def glb_joints(path):
    with open(path, "rb") as f:
        raw = f.read()
    json_len = struct.unpack("<I", raw[12:16])[0]
    gltf = json.loads(raw[20 : 20 + json_len])
    joints = []
    for skin in gltf.get("skins", []):
        joints += [gltf["nodes"][j].get("name") for j in skin["joints"]]
    return joints


bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__

if os.path.exists(TMP):
    shutil.rmtree(TMP)
os.makedirs(TMP)
MESH = os.path.join(TMP, "synth.glb")
LM = os.path.join(TMP, "lm.json")
FITTED = os.path.join(TMP, "fitted.py")

# 1. Synthetic subject -> GLB file (the fit/validate CLIs take paths).
dl = load_tool("ap_detect_landmarks", "detect_landmarks.py")
synth, _spec = dl.build_synthetic()
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
synth.select_set(True)
bpy.context.view_layer.objects.active = synth
bpy.ops.export_scene.gltf(filepath=MESH, export_format="GLB", use_selection=True)
check(os.path.exists(MESH), "synthetic GLB export failed")
print(f"synthetic mesh: {MESH} ({os.path.getsize(MESH)} bytes)")

# 2. Landmarks via the Phase-1 path (import + envelope + slices).
sig_result = dl.slice_signature(dl.envelope_copy(dl.import_mesh(MESH)), 120)
landmarks = dl.detect_biped(sig_result[0], sig_result[1], sig_result[2], "-Y")
with open(LM, "w", encoding="utf-8") as f:
    json.dump(landmarks, f, indent=2)
print(f"landmarks: wrist_z={landmarks['wrist_z']} ankle_z={landmarks['ankle_z']}")

# 3. Fit (includes headless generate) and prove the fit-delta numbers.
fit_out, fit_code = run_tool(
    "fit_metarig.py",
    [MESH, LM, "--out", FITTED, "--blend", os.path.join(TMP, "fit.blend")],
)
check(fit_code is None, f"fit_metarig exited {fit_code!r}")
check("RIGFORGE_FIT_OK" in fit_out, "fit report missing RIGFORGE_FIT_OK")
fit = parse_report(fit_out, "RIGFORGE_FIT_BEGIN", "RIGFORGE_FIT_OK")
check(os.path.exists(FITTED), "fitted metarig module not emitted")
print("fit-delta (before -> after, m):")
for joint, numbers in sorted(fit["joints"].items()):
    print(f"  {joint}: {numbers['before_d']} -> {numbers['after_d']}")
    check(numbers["after_d"] < 1e-3, f"{joint} not closed: {numbers['after_d']}")
check(
    fit["joints"]["wrist"]["before_d"] > 0.05,
    "wrist started within 5 cm: fit would prove nothing",
)
gen = fit["generate"]
check(gen and gen["def_bones"] > 30, f"generate proof weak: {gen}")
print(f"generate: rig bones={gen['rig_bones']} def={gen['def_bones']}")

# 4. Game export of the generated rig: deform-only skeleton.
rig = next(
    (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data),
    None,
)
check(rig is not None, "no generated rig in session after fit")
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
skel_path = os.path.join(TMP, "skel.glb")
bpy.ops.wm.rigforge_game_export(filepath=skel_path)
joints = glb_joints(skel_path)
bad = [j for j in joints if not (j or "").startswith("DEF-")]
check(joints, "exported GLB has no joints")
check(not bad, f"non-DEF joints exported: {bad[:5]}")
check(
    len(joints) == gen["def_bones"],
    f"GLB joints {len(joints)} != DEF bones {gen['def_bones']}",
)
print(f"export OK ({len(joints)} joints, all DEF-)")

# 5. In-session gate on the fitted mesh + metarig.
rv = load_tool("ap_validate_fit", "validate_fit.py")
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
meta = next(
    (o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" not in o.data),
    None,
)
check(meshes and meta is not None, "fit session lost its mesh/metarig")
mesh = max(meshes, key=lambda o: len(o.data.vertices))
env = dl.envelope_copy(mesh)
signature, _, _ = dl.slice_signature(env, 120)
gate = rv.run_gate(env, meta, landmarks, signature, open_allow=OPEN)
check(gate["verdict"] == "pass", f"in-session gate failed: {gate['failures']}")
inside = gate["inside"]
print(
    f"gate: {inside['checked']} drivers, min depth {inside['min_depth']}, "
    f"mean {inside['mean_depth']}, worst {inside['worst_bone']}"
)
check(inside["min_depth"] >= 0.01, "driver inside the 1 cm margin band")
for side in ("L", "R"):
    finger = gate["warns"]["fingers"][side]
    toe = gate["warns"]["toes"][side]
    check(finger["status"] == "ok", f"finger warn on synthetic: {finger}")
    check(toe["status"] == "ok", f"toe warn on synthetic: {toe}")
    print(
        f"  {side}: finger worst {finger['worst_deg']} deg, "
        f"toe overshoot {toe['overshoot']}"
    )
head = gate["warns"]["head"]
check(head["status"] == "ok", f"head warn on synthetic: {head}")
print(f"  head top {head['head_top']} vs skullcap {head['skullcap_z']}")

# 5b. New warn checks, good side: quiet on the healthy synthetic fit.
for side in ("L", "R"):
    finger = gate["warns"]["fingers"][side]
    check(
        finger["angles_deg"]["thumb"] < rv.FINGER_WARN_DEG,
        f"thumb angle warn on synthetic: {finger['angles_deg']}",
    )
    toe = gate["warns"]["toes"][side]
    check(toe["yaw_deg"] < rv.TOE_DIR_WARN, f"toe dir warn: {toe}")
    print(
        f"  {side}: finger pos {finger['worst_pos_m']} m, "
        f"thumb {finger['angles_deg']['thumb']} deg, "
        f"toe yaw {toe['yaw_deg']} deg, shortfall {toe['shortfall']}"
    )
face = gate["warns"]["face"]
check(face["status"] == "ok", f"face warn on synthetic: {face}")
print(f"  face brow clearance {face['brow_clearance']}, frac {face['face_frac']}")


# 5c. Red side: each new check fires on a known-bad metarig copy (only
# the warns are read; closure/symmetry/margin may fail on the copies).
def dup_meta(name):
    dup = meta.copy()
    dup.data = meta.data.copy()
    dup.name = name
    bpy.context.collection.objects.link(dup)
    bpy.context.view_layer.objects.active = dup
    bpy.ops.object.mode_set(mode="EDIT")
    return dup


shifted = dup_meta("ap_bad_fingers")
moved = 0
for stem in ("f_index", "f_middle", "f_ring", "f_pinky"):
    for mid in ("01", "02", "03"):
        b = shifted.data.edit_bones.get(f"{stem}.{mid}.L")
        check(b is not None, f"missing {stem}.{mid}.L for perturbation")
        b.head.x -= 0.03  # medial, out of the hand box (the Andras bias)
        b.tail.x -= 0.03
        moved += 1
check(moved == 12, f"shifted {moved} finger bones, expected 12")
bad_fingers = rv.run_gate(env, shifted, landmarks, signature, open_allow=OPEN)
bad_l = bad_fingers["warns"]["fingers"]["L"]
check(bad_l["status"] == "warn", f"shifted fan not caught: {bad_l}")
check("outside" in (bad_l["warn_cause"] or ""), f"wrong cause: {bad_l}")
check(
    bad_fingers["warns"]["fingers"]["R"]["status"] == "ok",
    "unshifted side warned",
)
check(
    all(a < rv.FINGER_WARN_DEG for a in bad_l["angles_deg"].values()),
    "angle check should be blind to pure translation (the old hole)",
)
print(f"  bad fingers: L {bad_l['warn_cause']} {bad_l['worst_pos_m']} m, R ok")

floated = dup_meta("ap_bad_face")
fb = rv.face_bones(floated.data.edit_bones)
check(fb, "face subtree missing for perturbation")
for b in fb:
    b.head.z += 0.08  # gross lift: centroid leaves the head blob
    b.tail.z += 0.08
bad_face = rv.run_gate(env, floated, landmarks, signature, open_allow=OPEN)["warns"][
    "face"
]
check(bad_face["status"] == "warn", f"floated face not caught: {bad_face}")
check("outside" in (bad_face["warn_cause"] or ""), f"wrong cause: {bad_face}")
print(f"  bad face: {bad_face['warn_cause']} centroid {bad_face['centroid']}")

twisted = dup_meta("ap_bad_toe")
tb = twisted.data.edit_bones.get("toe.L")
check(tb is not None, "toe.L missing for perturbation")
dx, dy, dz = tb.tail.x - tb.head.x, tb.tail.y - tb.head.y, tb.tail.z - tb.head.z
tb.tail = (tb.head.x - dy, tb.head.y + dx, tb.head.z + dz)  # yaw +90 deg
bad_toe = rv.run_gate(env, twisted, landmarks, signature, open_allow=OPEN)["warns"][
    "toes"
]["L"]
check(bad_toe["status"] == "warn", f"twisted toe not caught: {bad_toe}")
check("direction" in (bad_toe["warn_cause"] or ""), f"wrong cause: {bad_toe}")
print(f"  bad toe: {bad_toe['warn_cause']} yaw {bad_toe['yaw_deg']} deg")

# 5d. Mystery-stick regression: the rebuilt metarig must carry no stray.
rebuilt = rv.build_metarig_from_module(FITTED)
rebuilt_names = {b.name for b in rebuilt.data.bones}
meta_names = {b.name for b in meta.data.bones}
check("Bone" not in rebuilt_names, "stray default bone in rebuilt metarig")
check(rebuilt_names == meta_names, "rebuilt bone set differs from fitted")
print(f"  rebuild: {len(rebuilt_names)} bones, no stray")

# 6. CLI gate on the rebuilt module (round-trip incl. %.4f rounding).
cli_report = os.path.join(TMP, "cli_report.json")
overlay_dir = os.path.join(TMP, "overlay")
cli_out, cli_code = run_tool(
    "validate_fit.py",
    [
        MESH,
        FITTED,
        LM,
        "--open",
        ",".join(OPEN),
        "--render",
        overlay_dir,
        "--report",
        cli_report,
    ],
)
check(cli_code is None, f"validate CLI exited {cli_code!r}")
check("RIGFORGE_VALIDATE_OK" in cli_out, "CLI gate did not print VALIDATE_OK")
cli = parse_report(cli_out, "RIGFORGE_VALIDATE_BEGIN", "RIGFORGE_VALIDATE_OK")
check(
    cli["verdict"] == "pass" and cli["ladder"] == "full_auto",
    f"CLI verdict/ladder wrong: {cli['verdict']}/{cli.get('ladder')}",
)
for key in ("front", "side", "joints"):
    check(
        cli["overlay"] and os.path.exists(cli["overlay"][key]),
        f"overlay {key} missing",
    )
with open(cli["overlay"]["joints"], encoding="utf-8") as f:
    projections = json.load(f)
check(
    set(projections) == {"front", "side"} and "toe.L" in projections["front"]["joints"],
    "overlay joints JSON malformed",
)
print(f"CLI gate: full_auto, overlay in {overlay_dir}")

# 7. Failure path: an impossible margin stops the pipeline naming bones.
fail_out, fail_code = run_tool(
    "validate_fit.py",
    [MESH, FITTED, LM, "--open", ",".join(OPEN), "--margin", "0.5", "--no-render"],
)
check(fail_code == 1, f"failing gate exited {fail_code!r}, expected 1")
check("RIGFORGE_VALIDATE_FAIL" in fail_out, "no VALIDATE_FAIL marker")
check("STOP:" in fail_out, "no STOP lines naming bones/fixes")
named = [b for b in rv.DRIVER_BONES if b in fail_out]
check(named, "failing gate named no driver bones")
print(f"fail-stop OK (exit 1, e.g. {named[0]} named)")

# 8. Ladder rung 2: preset as-is + report on explicit request.
rung_out, rung_code = run_tool(
    "validate_fit.py",
    [
        MESH,
        FITTED,
        LM,
        "--open",
        ",".join(OPEN),
        "--margin",
        "0.5",
        "--no-render",
        "--on-fail",
        "preset",
    ],
)
check(rung_code is None, f"preset fallback exited {rung_code!r}")
check("RIGFORGE_VALIDATE_PRESET" in rung_out, "no VALIDATE_PRESET marker")
rung = parse_report(rung_out, "RIGFORGE_VALIDATE_BEGIN", "RIGFORGE_VALIDATE_PRESET")
check(rung["ladder"] == "preset_as_is", "ladder verdict not preset_as_is")
check("fallback_preset" in rung, "no fallback preset report attached")
print(f"ladder rung 2 OK (preset verdict: {rung['fallback_preset']['verdict']})")

# 9. Production regression subjects (read-only external meshes, Section 9).
# Andras: toe margin 7.2/8.5 mm + pinky warn; elf: shoulder/foot/toe
# outside (ponytail-dragged elbow, flat-shoe ankle); millennial: merge
# case (shoulder/hand outside, upper-arm margin). Absent files skip.
SUBJECTS = [
    ("andras", "/tmp/bakeoff/andras_body_raw.glb"),
    ("elf", os.path.expanduser("~/Downloads/hunyuan_elf.glb")),
    ("mill", os.path.expanduser("~/Downloads/hunyuan_millennial.glb")),
]
for tag, mesh_path in SUBJECTS:
    if not os.path.exists(mesh_path):
        print(f"subject {tag}: SKIP (missing {mesh_path})")
        continue
    s_tmp = os.path.join(TMP, tag)
    os.makedirs(s_tmp, exist_ok=True)
    s_lm = os.path.join(s_tmp, "lm.json")
    s_fitted = os.path.join(s_tmp, "fitted.py")
    lm_out, lm_code = run_tool("detect_landmarks.py", [mesh_path])
    check(lm_code is None, f"{tag}: landmarks exited {lm_code!r}")
    s_landmarks = parse_report(
        lm_out, "RIGFORGE_LANDMARKS_BEGIN", "RIGFORGE_LANDMARKS_OK"
    )
    with open(s_lm, "w", encoding="utf-8") as f:
        json.dump(s_landmarks, f, indent=2)
    fit_out, fit_code = run_tool("fit_metarig.py", [mesh_path, s_lm, "--out", s_fitted])
    check(fit_code is None, f"{tag}: fit exited {fit_code!r}")
    s_fit = parse_report(fit_out, "RIGFORGE_FIT_BEGIN", "RIGFORGE_FIT_OK")
    for joint, numbers in s_fit["joints"].items():
        check(numbers["after_d"] < 1e-3, f"{tag}: {joint} not closed")
    s_gen = s_fit["generate"]
    check(s_gen and s_gen["def_bones"] > 30, f"{tag}: generate weak: {s_gen}")
    s_meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    s_meta = next(
        (
            o
            for o in bpy.data.objects
            if o.type == "ARMATURE" and "rig_id" not in o.data
        ),
        None,
    )
    check(s_meshes and s_meta is not None, f"{tag}: session lost mesh/metarig")
    s_env = dl.envelope_copy(max(s_meshes, key=lambda o: len(o.data.vertices)))
    s_sig = dl.slice_signature(s_env, 120)
    s_gate = rv.run_gate(s_env, s_meta, s_landmarks, s_sig[0], open_allow=OPEN)
    check(s_gate["verdict"] == "pass", f"{tag}: gate failed: {s_gate['failures']}")
    check(s_gate["inside"]["min_depth"] >= 0.01, f"{tag}: driver in margin band")
    s_rebuilt = rv.build_metarig_from_module(s_fitted)
    s_names = {b.name for b in s_rebuilt.data.bones}
    check("Bone" not in s_names, f"{tag}: stray bone in rebuilt metarig")
    check(
        s_names == {b.name for b in s_meta.data.bones},
        f"{tag}: rebuilt bone set differs",
    )
    s_bones = s_gate["inside"]["bones"]
    if tag == "andras":
        check(
            s_bones["toe.L"]["mid"] >= 0.01 and s_bones["toe.R"]["mid"] >= 0.01,
            "andras: toe margin regressed",
        )
        for side in ("L", "R"):
            finger = s_gate["warns"]["fingers"][side]
            check(finger["status"] == "ok", f"andras: finger warn: {finger}")
    elif tag == "elf":
        for bone in (
            "shoulder.L",
            "shoulder.R",
            "foot.L",
            "foot.R",
            "toe.L",
            "toe.R",
        ):
            check(s_bones[bone]["inside"], f"elf: {bone} outside again")
        check(s_fit["fallbacks"]["elbow"]["fired"], "elf: elbow re-derive did not fire")
        check(
            s_fit["fallbacks"]["ankle"]["forefoot_trigger"],
            "elf: forefoot ankle trigger did not fire",
        )
        for side in ("L", "R"):
            finger = s_gate["warns"]["fingers"][side]
            check(finger["status"] == "ok", f"elf: finger warn: {finger}")
    elif tag == "mill":
        for bone in ("shoulder.L", "shoulder.R", "hand.L", "hand.R"):
            check(s_bones[bone]["inside"], f"mill: {bone} outside again")
        check(
            s_fit["fallbacks"]["hand"]["mesh_fit"], "mill: hand mesh-fit did not fire"
        )
        check(s_bones["toe.R"]["mid"] >= 0.01, "mill: toe.R margin regressed")
        for side in ("L", "R"):
            finger = s_gate["warns"]["fingers"][side]
            check(
                "outside" not in (finger.get("warn_cause") or ""),
                f"mill: finger tips outside: {finger}",
            )
            check(finger["worst_deg"] < 50, f"mill: finger angle worse: {finger}")
    print(
        f"subject {tag}: PASS (min {s_gate['inside']['min_depth']} "
        f"worst {s_gate['inside']['worst_bone']} def {s_gen['def_bones']})"
    )

print("RIGFORGE_AUTOPLACE_OK")
