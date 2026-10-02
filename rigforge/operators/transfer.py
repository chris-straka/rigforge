# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Piece transfer operator (thin UI over weights.transfer, W3).

Active mesh is the piece (cape, hair, armor); Source names the body
object. Near verts copy the body's weights, far verts are inpainted
from them. Copies the body's armature modifier too, so the piece
deforms with the same rig. Undoable.
"""

import bpy
from bpy.props import FloatProperty, StringProperty

from ..weights import nudge as _nudge
from ..weights import transfer as _transfer
from ..weights import voxel as _voxel


# noinspection PyPep8Naming
class WM_OT_rigforge_piece_transfer(bpy.types.Operator):
    bl_idname = "wm.rigforge_piece_transfer"
    bl_label = "Transfer Piece Weights"
    bl_description = (
        "Copy weights from the source body where the piece is close, "
        "inpaint smoothly where it hangs away"
    )
    bl_options = {"REGISTER", "UNDO"}

    source: StringProperty(
        name="Source",
        description="Body object to copy weights from",
        default="",
    )
    max_dist: FloatProperty(
        name="Max Distance",
        description="Copy radius in meters (0 = auto: 5% of body size)",
        default=0.0,
        min=0.0,
        subtype="DISTANCE",
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == "MESH")

    def execute(self, context):
        piece = context.active_object
        if piece is None or piece.type != "MESH":
            self.report({"ERROR"}, "Active object must be a mesh")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if not self.source:
            self.report({"ERROR"}, "Pick a source body first")
            return {"CANCELLED"}
        body = bpy.data.objects.get(self.source)
        if body is None or body.type != "MESH":
            self.report({"ERROR"}, f"Source '{self.source}' is not a mesh")
            return {"CANCELLED"}
        if body is piece:
            self.report({"ERROR"}, "Source must differ from the piece")
            return {"CANCELLED"}
        if not body.vertex_groups:
            self.report({"ERROR"}, f"Source '{self.source}' has no weights")
            return {"CANCELLED"}
        assignment, report = _transfer.solve_transfer(
            body, piece, max_dist=self.max_dist
        )
        _nudge.apply_nudge(piece, assignment)
        _voxel.fill_unassigned(piece)
        for mod in body.modifiers:
            if mod.type == "ARMATURE" and mod.object is not None:
                _voxel.ensure_armature_modifier(piece, mod.object)
                break
        self.report(
            {"INFO"},
            f"Transferred {report['close']} close + {report['far']} "
            f"inpainted verts (radius {report['threshold']:.3f} m)",
        )
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_piece_transfer)
    bpy.types.WindowManager.rigforge_transfer_source = StringProperty(
        name="Source",
        description="Body object to copy weights from",
        default="",
    )
    bpy.types.WindowManager.rigforge_transfer_dist = FloatProperty(
        name="Max Distance",
        description="Copy radius in meters (0 = auto: 5% of body size)",
        default=0.0,
        min=0.0,
        subtype="DISTANCE",
    )


def unregister():
    del bpy.types.WindowManager.rigforge_transfer_dist
    del bpy.types.WindowManager.rigforge_transfer_source
    bpy.utils.unregister_class(WM_OT_rigforge_piece_transfer)
