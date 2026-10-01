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

print("RIGFORGE_AUTOPLACE_OK")
