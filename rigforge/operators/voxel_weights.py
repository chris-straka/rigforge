# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Voxel auto-weights operator (thin UI over weights.voxel, W1).

Parent the mesh to the rig first (Parent > Armature is enough — no
weights needed), keep the mesh active, run wm.rigforge_voxel_weights:
replaces the mesh's vertex groups with geodesic voxel binding (works
on any closed mesh, never bleeds across air gaps), gap-fills
unweighted verts, and keeps the Armature modifier.
"""

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty

from ..auto_place import detect_landmarks as _detect
from ..weights import voxel as _voxel


def _find_armature(context, mesh):
    for mod in mesh.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj
    return None


# noinspection PyPep8Naming
class WM_OT_rigforge_voxel_weights(bpy.types.Operator):
    bl_idname = "wm.rigforge_voxel_weights"
    bl_label = "Voxel Auto-Weights"
    bl_description = (
        "Skin the active mesh with geodesic voxel binding "
        "(robust automatic weights, no bleed across gaps)"
    )
    bl_options = {"REGISTER", "UNDO"}

    cell_size: FloatProperty(
        name="Cell Size",
        description="Voxel pitch in meters (0 = automatic from mesh size)",
        default=0.0,
        min=0.0,
        max=0.1,
        precision=4,
    )
    max_influences: IntProperty(
        name="Max Influences",
        description="Bones kept per vertex (4 fits the Godot/mobile budget)",
        default=4,
        min=1,
        max=8,
    )
    smooth_iterations: IntProperty(
        name="Smooth",
        description="Laplacian smoothing passes per bone weight field "
        "(0 = automatic: constant physical blend width)",
        default=0,
        min=0,
        max=200,
    )
    use_envelope: BoolProperty(
        name="Envelope Source",
        description="Voxelize a fused-exterior remesh copy (for stacked-shell "
        "production meshes) instead of the mesh itself",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == "MESH")

    def execute(self, context):
        mesh = context.active_object
        if mesh is None or mesh.type != "MESH":
            self.report({"ERROR"}, "Active object must be a mesh")
            return {"CANCELLED"}
        rig = _find_armature(context, mesh)
        if rig is None:
            self.report(
                {"ERROR"},
                "No rig found: parent the mesh to an armature first "
                "(or select the armature too)",
            )
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        source = None
        try:
            if self.use_envelope:
                source = _detect.envelope_copy(mesh)
            assignment, report = _voxel.bind_weights(
                mesh,
                rig,
                cell=self.cell_size or None,
                source_obj=source,
                k=self.max_influences,
                smooth_iters=self.smooth_iterations or None,
            )
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            if source is not None:
                try:
                    data = source.data
                    bpy.data.objects.remove(source, do_unlink=True)
                    if data.users == 0:
                        bpy.data.meshes.remove(data)
                except ReferenceError:
                    pass
                mesh.select_set(True)
                bpy.context.view_layer.objects.active = mesh
        _voxel.apply_weights(mesh, assignment)
        _voxel.ensure_armature_modifier(mesh, rig)
        filled = _voxel.fill_unassigned(mesh)
        self.report(
            {"INFO"},
            f"Voxel weights: {report['bones']} bones, "
            f"{report['interior_voxels']} voxels, "
            f"{report['fallback_verts']} fallbacks, {filled} gap-filled",
        )
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_voxel_weights)


def unregister():
    bpy.utils.unregister_class(WM_OT_rigforge_voxel_weights)
