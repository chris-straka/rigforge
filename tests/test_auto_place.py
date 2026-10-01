# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: wm.rigforge_auto_place on synthetic hero + stalker.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_auto_place.py

Proves the operator fits the active metarig onto the selected mesh
in-session (the shared rigforge.auto_place solve, not a copy), passes
the gate, leaves no temp objects, is deterministic (a second run is a
no-op), and fails loud with the metarig untouched (no mesh selected,
wrong active type, missing bones, preset mismatch).

Pass criteria: exits 0 and prints RIGFORGE_AUTOPLACE_UI_OK.
"""

import sys

import bpy

OPEN = (
    "shoulder.L",
    "shoulder.R",
    "spine.004",
    "thigh.L",
    "thigh.R",
    "upper_arm.L",
    "upper_arm.R",
)
Q_OPEN = (
    "neck.001",
    "pelvis.L",
    "pelvis.R",
    "shoulder.L",
    "shoulder.R",
    "tail.001",
    "thigh.L",
    "thigh.R",
    "upper_arm.L",
    "upper_arm.R",
)


def fail(msg):
    print("RIGFORGE_AUTOPLACE_UI_FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def bone_map(meta):
    return {
        b.name: (
            b.head_local.copy(),
            b.tail_local.copy(),
            b.matrix_local.col[2].to_3d().copy(),
        )
        for b in meta.data.bones
    }


def max_drift(before, after):
    worst = 0.0
    check(set(before) == set(after), "bone set changed across runs")
    for name in before:
        hb, tb, _ = before[name]
        ha, ta, _ = after[name]
        worst = max(worst, (hb - ha).length, (tb - ta).length)
    return worst


def select_for_place(mesh, meta):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta


bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402
from rigforge.auto_place import detect_landmarks as dl  # noqa: E402
from rigforge.auto_place import validate_fit as rv  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_auto_place"), "operator not registered")

# --- Hero: fit + gate + determinism + cleanup ---
synth, _spec = dl.build_synthetic()
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.rigforge_hll_hero_metarig_add()
hero = bpy.context.view_layer.objects.active
stock = bone_map(hero)
select_for_place(synth, hero)
result = bpy.ops.wm.rigforge_auto_place()
check("FINISHED" in result, f"hero operator did not finish: {result}")
moved = max_drift(stock, bone_map(hero))
check(moved > 0.001, f"hero metarig did not move ({moved})")
print(f"hero placed: max bone move {moved:.4f} m")

env = dl.envelope_copy(synth)
signature, zmin, zmax = dl.slice_signature(env, 120)
landmarks = dl.detect_biped(signature, zmin, zmax, "-Y")
landmarks["kind"] = "biped"
gate = rv.run_gate(env, hero, landmarks, signature, open_allow=OPEN)
check(gate["verdict"] == "pass", f"hero gate: {gate['failures']}")
print(
    f"hero gate: PASS ({gate['inside']['checked']} drivers, "
    f"min depth {gate['inside']['min_depth'] * 1000:.1f} mm)"
)

placed = bone_map(hero)
select_for_place(synth, hero)
result = bpy.ops.wm.rigforge_auto_place()
check("FINISHED" in result, f"hero re-run did not finish: {result}")
drift = max_drift(placed, bone_map(hero))
check(drift < 1e-6, f"second run not a no-op (drift {drift})")
print(f"hero deterministic: re-run drift {drift:.2e} m")

temps = [o.name for o in bpy.data.objects if "autoplace" in o.name]
check(not temps, f"temp objects left behind: {temps}")

# --- Stalker: fit + gate on the synthetic quadruped ---
bpy.ops.object.mode_set(mode="OBJECT")
qsynth, _qspec = dl.build_synthetic_quadruped()
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.rigforge_hll_stalker_metarig_add()
stalker = bpy.context.view_layer.objects.active
qstock = bone_map(stalker)
select_for_place(qsynth, stalker)
result = bpy.ops.wm.rigforge_auto_place()
check("FINISHED" in result, f"stalker operator did not finish: {result}")
qmoved = max_drift(qstock, bone_map(stalker))
check(qmoved > 0.001, f"stalker metarig did not move ({qmoved})")
print(f"stalker placed: max bone move {qmoved:.4f} m")

qenv = dl.envelope_copy(qsynth)
long_sig, qymin, qymax = dl.slice_signature_axis(qenv, 120, "y")
trans_sig, qzmin, qzmax = dl.slice_signature_axis(qenv, 120, "z")
qx = [v.co.x for v in qenv.data.vertices]
qbounds = (min(qx), max(qx), qymin, qymax, qzmin, qzmax)
qlandmarks = dl.detect_quadruped(long_sig, trans_sig, qbounds, "-Y")
qsig, _, _ = dl.slice_signature(qenv, 120)
qgate = rv.run_gate(qenv, stalker, qlandmarks, qsig, open_allow=Q_OPEN)
check(qgate["verdict"] == "pass", f"stalker gate: {qgate['failures']}")
print(
    f"stalker gate: PASS ({qgate['inside']['checked']} drivers, "
    f"min depth {qgate['inside']['min_depth'] * 1000:.1f} mm)"
)

# --- Failure paths: loud cancels, metarig untouched ---
# Fresh fixtures: the quadruped builder above wiped the hero scene.
synth, _spec = dl.build_synthetic()
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.rigforge_hll_hero_metarig_add()
hero = bpy.context.view_layer.objects.active


def expect_cancelled(fn, needle):
    """Error-reporting cancels surface as RuntimeError with the message."""
    try:
        fn()
    except RuntimeError as exc:
        check(needle in str(exc), f"cancel message {exc!r} lacks {needle!r}")
    else:
        fail(f"expected RuntimeError containing {needle!r}")


select_for_place(synth, hero)
bpy.ops.object.select_all(action="DESELECT")
hero.select_set(True)
bpy.context.view_layer.objects.active = hero
before = bone_map(hero)
expect_cancelled(bpy.ops.wm.rigforge_auto_place, "Select the subject mesh")
check(max_drift(before, bone_map(hero)) == 0.0, "no-mesh run mutated bones")

bpy.ops.object.select_all(action="DESELECT")
synth.select_set(True)
hero.select_set(True)
bpy.context.view_layer.objects.active = synth
check(
    bpy.ops.wm.rigforge_auto_place.poll() is False,
    "operator should not poll with a mesh active",
)

bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.rigforge_hll_hero_metarig_add()
broken = bpy.context.view_layer.objects.active
bpy.ops.object.mode_set(mode="EDIT")
broken.data.edit_bones.remove(broken.data.edit_bones["foot.L"])
select_for_place(synth, broken)
expect_cancelled(bpy.ops.wm.rigforge_auto_place, "foot.L")

select_for_place(synth, hero)
expect_cancelled(
    lambda: bpy.ops.wm.rigforge_auto_place(preset="HLL_STALKER"),
    "Preset=hll_stalker",
)

bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
cube = bpy.context.view_layer.objects.active
select_for_place(cube, hero)
before = bone_map(hero)
expect_cancelled(bpy.ops.wm.rigforge_auto_place, "no biped signature")
check(max_drift(before, bone_map(hero)) == 0.0, "bad-mesh run mutated bones")

from rigforge.operators import auto_place as ap  # noqa: E402

snap = ap._snapshot_bones(hero)
bpy.ops.object.mode_set(mode="EDIT")
hero.data.edit_bones["upper_arm.L"].head.z += 0.5
bpy.ops.object.mode_set(mode="OBJECT")
ap._restore_bones(hero, snap)
check(max_drift(before, bone_map(hero)) == 0.0, "restore did not revert")

print("RIGFORGE_AUTOPLACE_UI_OK")
