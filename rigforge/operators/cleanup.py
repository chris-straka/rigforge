# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Cleanup operator (thin UI over weights.cleanup, W4).

Whole-mesh weight hygiene: limit influences per vert (Godot/mobile
budgets), normalize, prune specks, mirror L/R, smooth. Undoable.
By default only the deform-bone groups of the mesh's rig are touched:
masks, cloth pins and other non-bone groups keep their weights.
"""

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty

from ..weights import cleanup as _cleanup
from ..weights import nudge as _nudge

SUBSET_ITEMS = [
    (
        "BONE_DEFORM",
        "Deform Bones",
        "Groups of the rig's deform bones (all groups if the mesh has no rig)",
    ),
    ("ALL", "All Groups", "Every vertex group, masks and pins included"),
]


# noinspection PyPep8Naming
class WM_OT_rigforge_cleanup(bpy.types.Operator):
    bl_idname = "wm.rigforge_cleanup"
    bl_label = "Clean Up Weights"
    bl_description = "Limit, normalize, prune, mirror, or smooth all weights"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=[
            ("LIMIT", "Limit", "Cap influences per vertex, renormalize"),
            ("NORMALIZE", "Normalize", "Scale every row to sum 1"),
            ("PRUNE", "Prune", "Drop influences under the threshold"),
            ("MIRROR", "Mirror", "Mirror weights across X (L<->R)"),
            ("SMOOTH", "Smooth", "Average weights with neighbors"),
        ],
        default="LIMIT",
    )
    limit: IntProperty(
        name="Limit",
        description="Max influences per vertex (mobile budget)",
        default=4,
        min=1,
        max=32,
    )
    threshold: FloatProperty(
        name="Threshold",
        description="Prune influences under this weight",
        default=0.01,
        min=0.0,
        max=0.5,
        subtype="FACTOR",
    )
    passes: IntProperty(
        name="Passes",
        description="Smoothing passes (fixed count: deterministic)",
        default=10,
        min=1,
        max=200,
    )
    subset: EnumProperty(
        name="Subset",
        description="Which vertex groups the cleanup reads and rewrites",
        items=SUBSET_ITEMS,
        default="BONE_DEFORM",
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
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if not mesh.vertex_groups:
            self.report({"ERROR"}, f"'{mesh.name}' has no weights to clean")
            return {"CANCELLED"}
        only = None
        if self.subset == "BONE_DEFORM":
            only = _nudge.deform_group_names(mesh)
        if only is not None and not any(g.name in only for g in mesh.vertex_groups):
            self.report(
                {"ERROR"},
                f"'{mesh.name}' has no deform-bone weights (Subset: All Groups "
                "cleans every group)",
            )
            return {"CANCELLED"}
        rows, _names = _nudge.read_weights(mesh, only)
        if self.mode == "LIMIT":
            rows = _cleanup.limit_rows(rows, k=self.limit)
            what = f"limited to {self.limit}"
        elif self.mode == "NORMALIZE":
            rows = _cleanup.normalize_rows(rows)
            what = "normalized"
        elif self.mode == "PRUNE":
            rows = _cleanup.prune_rows(rows, threshold=self.threshold)
            what = f"pruned under {self.threshold:g}"
        elif self.mode == "MIRROR":
            rows = _cleanup.mirror_rows(rows, mesh)
            what = "mirrored L<->R"
        else:
            offsets, neighbors = _nudge.mesh_adjacency(mesh)
            rows = _cleanup.smooth_rows(rows, offsets, neighbors, self.passes)
            what = f"smoothed ({self.passes} passes)"
        _nudge.write_weights(mesh, rows, only)
        self.report({"INFO"}, f"Weights {what} on '{mesh.name}'")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_cleanup)
    bpy.types.WindowManager.rigforge_cleanup_limit = IntProperty(
        name="Limit",
        description="Max influences per vertex (mobile budget)",
        default=4,
        min=1,
        max=32,
    )
    bpy.types.WindowManager.rigforge_cleanup_threshold = FloatProperty(
        name="Threshold",
        description="Prune influences under this weight",
        default=0.01,
        min=0.0,
        max=0.5,
        subtype="FACTOR",
    )
    bpy.types.WindowManager.rigforge_cleanup_passes = IntProperty(
        name="Passes",
        description="Smoothing passes (fixed count: deterministic)",
        default=10,
        min=1,
        max=200,
    )
    bpy.types.WindowManager.rigforge_cleanup_subset = EnumProperty(
        name="Subset",
        description="Which vertex groups the cleanup reads and rewrites",
        items=SUBSET_ITEMS,
        default="BONE_DEFORM",
    )


def unregister():
    del bpy.types.WindowManager.rigforge_cleanup_subset
    del bpy.types.WindowManager.rigforge_cleanup_passes
    del bpy.types.WindowManager.rigforge_cleanup_threshold
    del bpy.types.WindowManager.rigforge_cleanup_limit
    bpy.utils.unregister_class(WM_OT_rigforge_cleanup)
