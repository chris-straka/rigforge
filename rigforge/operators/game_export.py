# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import re

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

# Mobile profile: DEF bone stems dropped as face (rigid with the head).
MOBILE_FACE_STEMS = frozenset(
    {
        "brow",
        "cheek",
        "chin",
        "ear",
        "eye",
        "forehead",
        "jaw",
        "lid",
        "lip",
        "nose",
        "teeth",
        "temple",
        "tongue",
    }
)
# Mobile profile: limb stems whose .NNN duplicates are twist segments.
MOBILE_TWIST_LIMBS = frozenset({"upper_arm", "forearm", "thigh", "shin"})


def classify_def_bone(name):
    """'face', 'twist', or 'keep' for a DEF bone name (mobile profile).

    Face matches the stem (DEF-jaw.L.001 -> jaw); twist matches limb
    stems with a numeric-duplicate suffix (DEF-thigh.L.001) so spine
    segments (DEF-spine.001) and fingers (DEF-f_index.01.L) survive.
    """
    stem = name[4:].split(".")[0].lower()
    if stem in MOBILE_FACE_STEMS:
        return "face"
    if stem in MOBILE_TWIST_LIMBS and re.search(r"\.\d{3}$", name):
        return "twist"
    return "keep"


def mobile_merge_targets(rig):
    """{dropped DEF bone: kept DEF bone} for the mobile profile.

    Target is the nearest kept DEF ancestor (twist, nested face).
    Face roots hang off controls with no DEF ancestor, so they ride
    the topmost kept DEF bone (max head Z, name tiebreak): on a Z-up
    rig that is the head/neck base, which is what the face should
    follow rigidly. Raw head distance is useless (rigs vary in
    scale; everything can sit within centimeters). Bones with no
    kept DEF anywhere are kept (never drop weights into the void).
    Non-DEF bones are never touched.
    """
    bones = rig.data.bones
    kept = [
        b
        for b in bones
        if b.name.startswith("DEF-") and classify_def_bone(b.name) == "keep"
    ]
    anchor = max(kept, key=lambda b: (b.head.z, b.name)) if kept else None
    targets = {}
    for bone in bones:
        if not bone.name.startswith("DEF-"):
            continue
        if classify_def_bone(bone.name) == "keep":
            continue
        parent = bone.parent
        while parent is not None and (
            not parent.name.startswith("DEF-")
            or classify_def_bone(parent.name) != "keep"
        ):
            parent = parent.parent
        if parent is not None:
            targets[bone.name] = parent.name
        elif anchor is not None:
            targets[bone.name] = anchor.name
    return targets


def merge_weights(mesh_obj, targets):
    """Fold dropped groups into their targets; returns verts remapped."""
    if not targets:
        return 0
    groups = mesh_obj.vertex_groups
    remapped = 0
    for dropped, target in targets.items():
        src = groups.get(dropped)
        if src is None:
            continue
        dst = groups.get(target)
        if dst is None:
            dst = groups.new(name=target)
        for vert in mesh_obj.data.vertices:
            try:
                w = src.weight(vert.index)
            except RuntimeError:
                continue
            if w <= 0.0:
                continue
            try:
                have = dst.weight(vert.index)
            except RuntimeError:
                have = 0.0
            dst.add([vert.index], min(have + w, 1.0), "REPLACE")
            remapped += 1
        groups.remove(src)
    return remapped


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
    profile: EnumProperty(
        name="Profile",
        description="Full skeleton, or mobile (face + twist bones merged up)",
        items=[
            ("FULL", "Full", "All deform bones"),
            ("MOBILE", "Mobile", "Drop face + twist bones, merge weights up"),
        ],
        default="FULL",
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
                {"WARNING"},
                f"No meshes bound to {rig.name}; exporting skeleton only",
            )

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        targets = mobile_merge_targets(rig) if self.profile == "MOBILE" else {}
        copies = []
        remapped = 0
        try:
            if targets:
                rig, meshes, remapped = self._mobile_copies(
                    context, rig, meshes, targets
                )
                copies = [rig, *meshes]
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
        finally:
            for obj in copies:
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and data.users == 0:
                    kind = (
                        bpy.data.armatures
                        if data.bl_rna.identifier == "Armature"
                        else bpy.data.meshes
                    )
                    kind.remove(data)

        if self.profile == "MOBILE":
            self.report(
                {"INFO"},
                f"Exported mobile {self.filepath} "
                f"({len(targets)} bones merged up, "
                f"{remapped} weights remapped)",
            )
        else:
            self.report({"INFO"}, f"Exported {self.filepath}")
        return {"FINISHED"}

    def _mobile_copies(self, context, rig, meshes, targets):
        """Temp export copies: dropped bones non-deforming, weights merged.

        The originals are never touched. Copies share the action/NLA so
        animation bakes identically for the kept bones.
        """
        collection = context.scene.collection
        rig_copy = rig.copy()
        rig_copy.data = rig.data.copy()
        collection.objects.link(rig_copy)
        for name in targets:
            bone = rig_copy.data.bones.get(name)
            if bone is not None:
                bone.use_deform = False
        if rig.animation_data is not None:
            rig_copy.animation_data_create()
            rig_copy.animation_data.action = rig.animation_data.action
            for track in rig.animation_data.nla_tracks:
                ntrack = rig_copy.animation_data.nla_tracks.new()
                ntrack.name = track.name
                ntrack.mute = track.mute
                for strip in track.strips:
                    dup = ntrack.strips.new(
                        strip.name, int(strip.frame_start), strip.action
                    )
                    dup.frame_end = strip.frame_end
                    dup.mute = strip.mute
        mesh_copies = []
        remapped = 0
        for mesh in meshes:
            dup = mesh.copy()
            dup.data = mesh.data.copy()
            collection.objects.link(dup)
            for mod in dup.modifiers:
                if mod.type == "ARMATURE" and mod.object == rig:
                    mod.object = rig_copy
            remapped += merge_weights(dup, targets)
            mesh_copies.append(dup)
        return rig_copy, mesh_copies, remapped


def register():
    bpy.utils.register_class(WM_OT_rigforge_game_export)


def unregister():
    bpy.utils.unregister_class(WM_OT_rigforge_game_export)
