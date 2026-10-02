# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: metarig diagnostics + wm.rigforge_diagnose_metarig.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_diagnose.py

Gates: every check fires on a crafted defect with the bone name and
a fix (unknown type suggests the right one), clean metarigs pass,
results deterministic, operator FINISHED/CANCELLED paths + poll,
read-only (no UNDO flag, armature byte-identical after), and the
stray warning is grounded (a stray really becomes a dead ORG bone).

Pass criteria: exits 0 and prints RIGFORGE_DIAGNOSE_OK.
"""

import os
import sys

sys.path.insert(0, "tests")
os.environ["RF_W1_ONLY"] = "none"

import bpy


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


bpy.ops.preferences.addon_enable(module="rigforge")

import rigforge  # noqa: E402
from rigforge.operators import diagnose as diag_mod  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__
check(hasattr(bpy.ops.wm, "rigforge_diagnose_metarig"), "operator not registered")


def make_metarig(spec):
    """spec: [(name, parent, rig_type, head, tail)]. Returns the object."""
    data = bpy.data.armatures.new("DiagMeta")
    obj = bpy.data.objects.new("DiagMeta", data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    ebs = obj.data.edit_bones
    for name, parent, _typ, head, tail in spec:
        eb = ebs.new(name)
        eb.head = head
        eb.tail = tail
        if parent is not None:
            eb.parent = ebs[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    for name, _parent, typ, _head, _tail in spec:
        obj.pose.bones[name].rigforge_type = typ
    return obj


def by_code(result, level):
    return {i["code"]: i for i in result[level]}


CLEAN = [
    ("root", None, "", (0, 0, 0), (0, 0, 0.2)),
    ("spine", "root", "spines.super_spine", (0, 0, 0.2), (0, 0, 1.0)),
    ("thigh.L", "spine", "limbs.super_limb", (0.1, 0, 1.0), (0.1, 0, 0.5)),
]

# --- Module: clean passes, deterministic, read-only ---
meta = make_metarig(CLEAN)
result = diag_mod.diagnose_metarig(meta)
check(result == {"errors": [], "warnings": []}, f"clean flagged: {result}")
check(diag_mod.diagnose_metarig(meta) == result, "diagnose not deterministic")
before = [(b.name, tuple(b.head), tuple(b.tail)) for b in meta.data.bones]
diag_mod.diagnose_metarig(meta)
after = [(b.name, tuple(b.head), tuple(b.tail)) for b in meta.data.bones]
check(before == after, "diagnose mutated the armature")
print("module: clean passes, deterministic, read-only")

# --- Module: every defect named with a fix ---
bad = make_metarig(
    [
        ("root", None, "", (0, 0, 0), (0, 0, 0.2)),
        ("spine", "root", "spines.super_spinne", (0, 0, 0.2), (0, 0, 1.0)),
    ]
)
errs = by_code(diag_mod.diagnose_metarig(bad), "errors")
check("unknown_type" in errs, "typo not caught")
check(errs["unknown_type"]["bone"] == "spine", "typo bone not named")
check("super_spine" in errs["unknown_type"]["message"], "no suggestion")
check("Rigforge Type" in errs["unknown_type"]["hint"], "hint not actionable")

rooted = make_metarig(
    [
        ("spine", None, "spines.super_spine", (0, 0, 0.2), (0, 0, 1.0)),
        ("root", "spine", "limbs.super_limb", (0, 0, 0), (0, 0, 0.2)),
    ]
)
errs = by_code(diag_mod.diagnose_metarig(rooted), "errors")
check("root_parented" in errs, "parented root not caught")
check(errs["root_parented"]["bone"] == "root", "root not named")
check("root_typed" in errs, "typed root not caught")
check("Clear" in errs["root_typed"]["hint"], "root hint not actionable")

bare = make_metarig([("bone", None, "", (0, 0, 0), (0, 0, 1.0))])
errs = by_code(diag_mod.diagnose_metarig(bare), "errors")
check("no_typed_bones" in errs, "untyped rig not caught")

warned = make_metarig(
    [
        *CLEAN,
        ("stray", None, "", (0, 0, 2.0), (0, 0, 2.5)),
        ("flat", "spine", "basic.raw_copy", (0, 0, 1.0), (0, 0, 1.0)),
        ("spacy", "spine", " basic.raw_copy ", (0, 0, 1.0), (0, 0, 1.5)),
    ]
)
result = diag_mod.diagnose_metarig(warned)
check(result["errors"] == [], f"warnings misgraded: {result['errors']}")
warns = by_code(result, "warnings")
check("untyped_ignored" in warns, "stray not flagged")
check(warns["untyped_ignored"]["bone"] == "stray", "stray not named")
check("zero_length" in warns, "zero-length not flagged")
check(warns["zero_length"]["bone"] == "flat", "flat bone not named")
check("spaced_type" in warns, "spaced type not flagged")
check("raw_copy" in warns["spaced_type"]["message"], "stripped form not shown")
print("module: every defect named with a fix")

# --- Operator: FINISHED / CANCELLED / poll / read-only flag ---
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.select_all(action="DESELECT")
meta.select_set(True)
bpy.context.view_layer.objects.active = meta
check(
    "FINISHED" in bpy.ops.wm.rigforge_diagnose_metarig(),
    "clean metarig did not finish",
)
bpy.ops.object.select_all(action="DESELECT")
bad.select_set(True)
bpy.context.view_layer.objects.active = bad
try:
    result = bpy.ops.wm.rigforge_diagnose_metarig()
except RuntimeError as exc:
    check("unknown Rig Type" in str(exc), f"wrong error: {exc}")
else:
    check("CANCELLED" in result, f"bad metarig did not cancel: {result}")
cube_data = bpy.data.meshes.new("DiagCube")
cube = bpy.data.objects.new("DiagCube", cube_data)
bpy.context.scene.collection.objects.link(cube)
bpy.ops.object.select_all(action="DESELECT")
cube.select_set(True)
bpy.context.view_layer.objects.active = cube
check(
    bpy.ops.wm.rigforge_diagnose_metarig.poll() is False,
    "operator should not poll on a mesh",
)
meta.data["rig_id"] = "fake"
bpy.ops.object.select_all(action="DESELECT")
meta.select_set(True)
bpy.context.view_layer.objects.active = meta
check(
    bpy.ops.wm.rigforge_diagnose_metarig.poll() is False,
    "operator should not poll on a generated rig",
)
del meta.data["rig_id"]
check(
    "UNDO" not in diag_mod.WM_OT_rigforge_diagnose_metarig.bl_options,
    "diagnose must stay read-only (no UNDO flag)",
)
print("operator: paths + poll + read-only flag OK")

# --- Grounding: the stray warning is true (generate ignores strays) ---
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.object.rigforge_human_metarig_add()
bpy.ops.object.mode_set(mode="OBJECT")
human = bpy.context.view_layer.objects.active
bpy.ops.object.mode_set(mode="EDIT")
stray = human.data.edit_bones.new("diag_stray")
stray.head = (0, 0, 3.0)
stray.tail = (0, 0, 3.5)
bpy.ops.object.mode_set(mode="OBJECT")
warns = by_code(diag_mod.diagnose_metarig(human), "warnings")
check(
    warns.get("untyped_ignored", {}).get("bone") == "diag_stray",
    "stray not flagged on the human metarig",
)
bpy.ops.pose.rigforge_generate()
rig = bpy.context.view_layer.objects.active
corpses = [b for b in rig.data.bones if "diag_stray" in b.name]
check(
    [b.name for b in corpses] == ["ORG-diag_stray"] and corpses[0].use_deform is False,
    f"stray did not become exactly one dead ORG bone: {corpses}",
)
print("grounding: stray flagged and really a dead ORG bone")

print("RIGFORGE_DIAGNOSE_OK")
