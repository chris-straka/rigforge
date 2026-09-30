# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper


def get_generated_rig(context):
    """Return the active object if it is a generated rigforge rig, else None."""
    obj = context.active_object
    if obj and obj.type == "ARMATURE" and "rig_id" in obj.data:
        return obj
    return None


def find_skinned_meshes(rig):
    """Mesh objects deformed by the rig via an Armature modifier."""
    meshes = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object == rig:
                meshes.append(obj)
                break
    return meshes


# noinspection PyPep8Naming
class WM_OT_rigforge_game_export(bpy.types.Operator, ExportHelper):
    bl_idname = "wm.rigforge_game_export"
    bl_label = "Export Game Rig GLB"
    bl_description = (
        "Export the active generated rig and its skinned meshes "
        "as a deform-bones-only GLB for game engines (Godot)"
    )

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    export_animations: BoolProperty(
        name="Animations",
        description="Export actions and NLA tracks as glTF animations",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return get_generated_rig(context) is not None

    def execute(self, context):
        rig = get_generated_rig(context)
        if rig is None:
            self.report({"ERROR"}, "Active object is not a generated rigforge rig")
            return {"CANCELLED"}

        if not hasattr(bpy.ops.export_scene, "gltf"):
            self.report(
                {"ERROR"}, "The glTF exporter addon (io_scene_gltf2) is not enabled"
            )
            return {"CANCELLED"}

        meshes = find_skinned_meshes(rig)
        if not meshes:
            self.report(
                {"WARNING"}, "No meshes bound to %s; exporting skeleton only" % rig.name
            )

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        prev_selected = list(context.selected_objects)
        prev_active = context.view_layer.objects.active
        try:
            bpy.ops.object.select_all(action="DESELECT")
            rig.select_set(True)
            for mesh in meshes:
                mesh.select_set(True)
            context.view_layer.objects.active = rig

            bpy.ops.export_scene.gltf(
                filepath=self.filepath,
                export_format="GLB",
                use_selection=True,
                export_yup=True,
                export_apply=False,
                export_skins=True,
                export_def_bones=True,
                export_animations=self.export_animations,
            )
        finally:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in prev_selected:
                try:
                    obj.select_set(True)
                except ReferenceError:
                    pass
            try:
                context.view_layer.objects.active = prev_active
            except ReferenceError:
                pass

        self.report({"INFO"}, "Exported %s" % self.filepath)
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_game_export)


def unregister():
    bpy.utils.unregister_class(WM_OT_rigforge_game_export)
