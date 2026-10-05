# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless test: the genforge repair-rig adapter (tools/genforge_adapter.py).

Run: tools/run_test.sh tests/test_genforge_adapter.py
(or python tools/run_test_bpy.py tests/test_genforge_adapter.py)

A synthetic hero with a deliberately broken one-bone rig goes in; the
adapter must drop that rig, auto-rig the mesh with hll_hero, write a
deform-only GLB and a result.json in genforge's schema (exit 0). Error
paths: custom class -> ok false (exit 1, result written); missing input
or unknown stage -> exit 2 with no result.

Pass criteria: exits 0 and prints RIGFORGE_GENFORGE_ADAPTER_OK.
"""

import importlib.util
import json
import os
import sys
import tempfile

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fail(msg):
    print("RIGFORGE_GENFORGE_ADAPTER_FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


spec = importlib.util.spec_from_file_location(
    "genforge_adapter", os.path.join(ROOT, "tools", "genforge_adapter.py")
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

bpy.ops.preferences.addon_enable(module="rigforge")
from rigforge.auto_place import detect_landmarks as dl  # noqa: E402

work = tempfile.mkdtemp(prefix="rf_genforge_test_")
broken = os.path.join(work, "broken.glb")

# Synthetic hero + a junk one-bone armature skinning everything.
synth, _spec = dl.build_synthetic()
bpy.ops.object.mode_set(mode="OBJECT")
arm_data = bpy.data.armatures.new("Junk")
arm = bpy.data.objects.new("Junk", arm_data)
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
bone = arm_data.edit_bones.new("NotDEF")
bone.head = (0, 0, 0)
bone.tail = (0, 0, 0.5)
bpy.ops.object.mode_set(mode="OBJECT")
group = synth.vertex_groups.new(name="NotDEF")
group.add(range(len(synth.data.vertices)), 1.0, "REPLACE")
mod = synth.modifiers.new("Armature", "ARMATURE")
mod.object = arm
synth.parent = arm
bpy.ops.object.select_all(action="DESELECT")
synth.select_set(True)
arm.select_set(True)
bpy.ops.export_scene.gltf(filepath=broken, export_format="GLB", use_selection=True)
check(os.path.getsize(broken) > 0, "fixture GLB not written")

step = os.path.join(work, "09-fix", "repair-rig")
os.makedirs(step)
out = os.path.join(step, "output.glb")
result = os.path.join(step, "result.json")
code, payload = adapter.run_adapter(
    ["repair-rig", broken, out, result, "--class", "humanoid"]
)
check(code == 0, f"adapter exit {code}: {payload}")
with open(result, encoding="utf-8") as f:
    written = json.load(f)
check(written["ok"] is True, f"ok false: {written}")
check(written["tool"] == "rigforge", "tool name")
check(written["outputs"] == ["output.glb"], f"outputs {written['outputs']}")
details = written["repair-rig"]
check(details["preset"] == "hll_hero", "preset")
check(details["bones_before"] == 1, f"bones_before {details['bones_before']}")
print(f"repair-rig: {details}")

# The rerigged GLB: DEF-only joints, the old bone gone, mesh skinned.
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=out)
arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
check(len(arms) == 1, f"expected one armature, got {len(arms)}")
names = [b.name for b in arms[0].data.bones]
check(len(names) >= 60, f"only {len(names)} bones")
check(all(n.startswith("DEF-") for n in names), "non-DEF bone exported")
check("NotDEF" not in names, "old bone survived")
meshes = [
    o for o in bpy.data.objects if o.type == "MESH" and o.find_armature() is not None
]
check(meshes, "no mesh bound to the new rig")
print(f"rerig: {len(names)} DEF bones, {len(meshes)} bound mesh(es)")

# custom class: ok false, result written, exit 1.
custom_result = os.path.join(work, "custom.json")
code, payload = adapter.run_adapter(
    [
        "repair-rig",
        broken,
        os.path.join(work, "c.glb"),
        custom_result,
        "--class",
        "custom",
    ]
)
check(code == 1 and payload["ok"] is False, f"custom: {code} {payload}")
check(os.path.isfile(custom_result), "custom result not written")
check(not os.path.exists(os.path.join(work, "c.glb")), "custom wrote a model")

# Errors: no result file.
for argv in (
    [
        "repair-rig",
        os.path.join(work, "missing.glb"),
        os.path.join(work, "m.glb"),
        os.path.join(work, "m.json"),
    ],
    ["explode", broken, os.path.join(work, "e.glb"), os.path.join(work, "e.json")],
    ["repair-rig", broken],
):
    code, payload = adapter.run_adapter(argv)
    check(code == 2 and payload is None, f"{argv[0]} error path: {code}")
check(not os.path.exists(os.path.join(work, "m.json")), "error wrote a result")

print("RIGFORGE_GENFORGE_ADAPTER_OK")
