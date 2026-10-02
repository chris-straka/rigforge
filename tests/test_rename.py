# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: rename completeness + coexistence with bundled Rigify.

Run (from the repo root, after syncing the overlay):
    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender \\
      --background --factory-startup --python tests/test_rename.py

Pass criteria: exits 0 and prints RIGFORGE_RENAME_TEST_OK.
"""

import re
import sys
from pathlib import Path

import bpy

# Lines containing these may legitimately mention the old name: third-party
# feature-set names, legacy generated-rig compat shims, on-disk data formats,
# and historical comments. Everything else must say Rigforge.
ALLOWLIST = (
    "Cessen's Rigify Extensions",
    "made by a Rigify maintainer",
    "legacy Rigify rigs minimally ported",
    "modern Rigify",
    "pre-2.76b) Rigify",
    "like the buildbot",
    "Rigify_Arm_",
    "Rigify_Leg_",
    "Rigify_Rot2PoleSwitch",
    "Rigify Snap ",
)

# Old-prefix *identifiers*: UPPER_SNAKE and ClassNames. Plain prose ("a Rigify
# maintainer") and legacy shim names ("Rigify_Arm_FK2IK") do not match.
PATTERNS = (
    re.compile(r"\bRIGIFY_[A-Z][A-Z_]*"),
    re.compile(r"\bRigify[A-Z]\w*"),
    re.compile(r"\brigify_[a-z]"),
)


def fail(msg):
    print("RIGFORGE_RENAME_TEST_FAIL:", msg)
    sys.exit(1)


# 1. Enable our copy of the addon (same gate as the smoke test).
bpy.ops.preferences.addon_enable(module="rigforge")
import rigforge  # noqa: E402

assert "rf_scripts" in rigforge.__file__, "loaded wrong copy: " + rigforge.__file__

# 2. Source scan: no old-prefix identifiers outside the allowlist.
addon_dir = Path(rigforge.__file__).parent
violations = []
for path in sorted(addon_dir.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if any(s in line for s in ALLOWLIST):
            continue
        for pattern in PATTERNS:
            if pattern.search(line):
                violations.append(f"{path.name}:{lineno}: {line.strip()}")
if violations:
    fail(f"{len(violations)} old-prefix identifiers:\n" + "\n".join(violations[:10]))
print(f"rename scan OK ({addon_dir.name}/)")

# 3. Coexistence: bundled Rigify and rigforge enabled at the same time.
# Any shared bl_idname / property / menu id fails the second registration.
try:
    bpy.ops.preferences.addon_enable(module="rigify")
except Exception as e:
    fail(f"cannot enable bundled rigify alongside rigforge: {e}")
for op in ("rigforge_generate", "rigify_generate"):
    if not hasattr(bpy.ops.pose, op):
        fail(f"bpy.ops.pose.{op} missing with both addons enabled")
for op in ("rigforge_human_metarig_add", "armature_human_metarig_add"):
    if not hasattr(bpy.ops.object, op):
        fail(f"bpy.ops.object.{op} missing with both addons enabled")
for op in ("rigforge_convert_rotation", "convert_rotation"):
    if not hasattr(bpy.ops.pose, op):
        fail(f"bpy.ops.pose.{op} missing: rotation-converter op replaced")
for op in ("rigforge_metarig_sample_add", "metarig_sample_add"):
    if not hasattr(bpy.ops.armature, op):
        fail(f"bpy.ops.armature.{op} missing: sample op replaced")
for prop in ("rigforge_active_feature_set", "active_feature_set"):
    if not hasattr(bpy.types.Armature, prop):
        fail(f"Armature.{prop} missing: feature-set prop replaced")
print("coexistence OK (rigify + rigforge both enabled)")

# 4. Both addons generate side by side (catches generated-rig id collisions).
GENERATE_CASES = (
    ("armature_human_metarig_add", "rigify_generate", "bundled"),
    ("rigforge_human_metarig_add", "rigforge_generate", "ours"),
)
for metarig_op, generate_op, label in GENERATE_CASES:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    getattr(bpy.ops.object, metarig_op)()
    bpy.ops.object.mode_set(mode="OBJECT")
    getattr(bpy.ops.pose, generate_op)()
    rigs = [o for o in bpy.data.objects if o.type == "ARMATURE" and o.name != "metarig"]
    if not rigs:
        fail(f"{label}: generate produced no rig")
    print(f"{label}: generate OK ({rigs[0].name}, {len(rigs[0].data.bones)} bones)")

# 5. Driver freeze prefix round-trip (incl. legacy RIGIFY- healing).
from types import SimpleNamespace  # noqa: E402

from rigforge.generate import Generator  # noqa: E402

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.object.armature_add()
ob = bpy.context.object
bone_name = ob.pose.bones[0].name
ob.pose.bones[0]["rf_probe"] = 1.0
fc = ob.driver_add("location", 0)
var = fc.driver.variables.new()
var.type = "SINGLE_PROP"
tar = var.targets[0]
tar.id = ob
path = f'pose.bones["{bone_name}"]["rf_probe"]'
tar.data_path = path
Generator._Generator__freeze_driver_vars(ob)
if tar.data_path != "RIGFORGE-" + path:
    fail(f"freeze prefix wrong: {tar.data_path}")
fake = SimpleNamespace(obj=ob, org_rename_table={})
Generator._Generator__restore_driver_vars(fake)
if tar.data_path != path:
    fail(f"restore did not strip: {tar.data_path}")
tar.data_path = "RIGIFY-" + path
Generator._Generator__restore_driver_vars(fake)
if tar.data_path != path:
    fail(f"legacy RIGIFY- not healed: {tar.data_path}")
print("driver freeze round-trip OK (RIGFORGE-, legacy RIGIFY- heals)")

print("RIGFORGE_RENAME_TEST_OK")
