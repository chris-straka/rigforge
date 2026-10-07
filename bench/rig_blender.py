# SPDX-License-Identifier: GPL-2.0-or-later
"""Blender side of bench/joints.py (headless).

    blender -b --factory-startup --python bench/rig_blender.py -- strip REF.glb MESH.glb TRUTH.json
    blender -b --factory-startup --python bench/rig_blender.py -- joints RIGGED.glb OUT.json
    blender -b --factory-startup --python bench/rig_blender.py -- metarig MESH.glb OUT.json

strip: the corpus character without its rig (the auto-rigger's input) plus
its MPFB2 skeleton's joint heads (the ground truth), world space, Z up.
joints: world joint heads of a rigged GLB (bone name -> [x, y, z]).
metarig: Blender's stock Rigify human metarig scaled to the mesh's height
and centred on it (the free starting point before hand placement); its
bone heads, named like rigforge's DEF bones.
"""

import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def heads(arm):
    mw = arm.matrix_world
    return {b.name: list(mw @ b.head_local) for b in arm.data.bones} | \
        {b.name + "@tail": list(mw @ b.tail_local) for b in arm.data.bones}


def strip(ref, mesh_out, truth):
    clear()
    bpy.ops.import_scene.gltf(filepath=ref)
    arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
    json.dump(heads(arm), open(truth, "w"), indent=1)
    meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o.visible_get()]
    for o in meshes:
        mw = o.matrix_world.copy()
        for m in list(o.modifiers):
            o.modifiers.remove(m)
        o.parent = None
        o.matrix_world = mw
        o.vertex_groups.clear()
    bpy.data.objects.remove(arm)
    bpy.ops.object.select_all(action='DESELECT')
    for o in meshes:
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=mesh_out, use_selection=True, export_materials='NONE',
                              export_skins=False, export_animations=False)


def joints(rigged, out):
    clear()
    bpy.ops.import_scene.gltf(filepath=rigged, bone_heuristic="TEMPERANCE")
    arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
    json.dump(heads(arm), open(out, "w"), indent=1)


def metarig(mesh, out):
    import addon_utils
    addon_utils.enable("rigify", default_set=True)
    clear()
    bpy.ops.import_scene.gltf(filepath=mesh)
    ms = [o for o in bpy.data.objects if o.type == 'MESH' and o.visible_get()]
    zs = [(o.matrix_world @ v.co) for o in ms for v in o.data.vertices]
    lo = min(p.z for p in zs)
    hi = max(p.z for p in zs)
    cx = (min(p.x for p in zs) + max(p.x for p in zs)) / 2
    cy = (min(p.y for p in zs) + max(p.y for p in zs)) / 2
    bpy.ops.object.armature_human_metarig_add()
    rig = bpy.context.active_object
    bz = [rig.matrix_world @ b.head_local for b in rig.data.bones] + [rig.matrix_world @ b.tail_local for b in rig.data.bones]
    rlo = min(p.z for p in bz)
    rhi = max(p.z for p in bz)
    rcx = (min(p.x for p in bz) + max(p.x for p in bz)) / 2
    rcy = (min(p.y for p in bz) + max(p.y for p in bz)) / 2
    k = (hi - lo) / (rhi - rlo)
    rig.scale = (k, k, k)
    rig.location = (cx - rcx * k, cy - rcy * k, lo - rlo * k)
    bpy.context.view_layer.update()
    h = heads(rig)
    json.dump({("DEF-" + n if not n.endswith("@tail") else "DEF-" + n): v for n, v in h.items()}, open(out, "w"), indent=1)


{"strip": strip, "joints": joints, "metarig": metarig}[argv[0]](*argv[1:])
