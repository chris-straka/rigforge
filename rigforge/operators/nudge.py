# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Nudge strokes operator (thin UI over weights.nudge, W2).

Select verts, pick a bone, Pin (this follows the bone at the value)
or Exclude (not this bone): the pick is stored on the mesh and every
bone's field re-solves smoothly around all stored pins. Repeat per
stroke; Clear drops the stored pins. Undoable per stroke.
"""

import json

import bpy
from bpy.props import EnumProperty, FloatProperty, StringProperty

from ..weights import nudge as _nudge
from ..weights import voxel as _voxel

PINS_KEY = "rigforge_nudge_pins"
EXCLUDE_SENTINEL = -1.0


def get_pins(mesh):
    """Stored pins as {vert_idx: {bone: value or None}} (None = excluded)."""
    raw = mesh.get(PINS_KEY, "")
    pins = {}
    if not raw:
        return pins
    for entry in json.loads(raw):
        vi, bone, value = int(entry[0]), str(entry[1]), float(entry[2])
        pins.setdefault(vi, {})[bone] = None if value == EXCLUDE_SENTINEL else value
    return pins


def set_pins(mesh, pins):
    # JSON string: ID properties reject mixed-type nested lists.
    flat = []
    for vi in sorted(pins):
        for bone in sorted(pins[vi]):
            value = pins[vi][bone]
            flat.append([vi, bone, EXCLUDE_SENTINEL if value is None else value])
    mesh[PINS_KEY] = json.dumps(flat)


# noinspection PyPep8Naming
class WM_OT_rigforge_nudge(bpy.types.Operator):
    bl_idname = "wm.rigforge_nudge"
    bl_label = "Nudge Weights"
    bl_description = (
        "Pin selected verts to a bone (or exclude one) and re-solve "
        "smooth weights everywhere else"
    )
    bl_options = {"REGISTER", "UNDO"}

    bone: StringProperty(
        name="Bone",
        description="Target bone (vertex-group name)",
        default="",
    )
    value: FloatProperty(
        name="Value",
        description="Pinned weight for the bone",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    mode: EnumProperty(
        name="Mode",
        items=[
            ("PIN", "Pin", "Pin selected verts to the bone at Value"),
            ("EXCLUDE", "Exclude", "Lock the bone at 0 on selected verts"),
            ("CLEAR", "Clear", "Drop all stored nudge pins"),
        ],
        default="PIN",
    )

    @classmethod
    def poll(cls, context):
        # Selection is checked in execute (poll cannot see the invoked
        # mode, and CLEAR needs no selection).
        obj = context.active_object
        return bool(obj and obj.type == "MESH")

    def execute(self, context):
        mesh = context.active_object
        if mesh is None or mesh.type != "MESH":
            self.report({"ERROR"}, "Active object must be a mesh")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if self.mode == "CLEAR":
            if PINS_KEY in mesh:
                del mesh[PINS_KEY]
            self.report({"INFO"}, "Nudge pins cleared")
            return {"FINISHED"}
        if not self.bone:
            self.report({"ERROR"}, "Pick a bone first")
            return {"CANCELLED"}
        if self.bone not in mesh.vertex_groups:
            self.report({"ERROR"}, f"Bone '{self.bone}' has no vertex group")
            return {"CANCELLED"}
        selected = [v.index for v in mesh.data.vertices if v.select]
        if not selected:
            self.report({"ERROR"}, "Select verts to nudge first")
            return {"CANCELLED"}
        pins = get_pins(mesh)
        pinval = None if self.mode == "EXCLUDE" else float(self.value)
        for vi in selected:
            pins.setdefault(vi, {})[self.bone] = pinval
        set_pins(mesh, pins)
        per_vert, _names = _nudge.read_weights(mesh)
        offsets, neighbors = _nudge.mesh_adjacency(mesh)
        assignment = _nudge.solve_nudge(per_vert, offsets, neighbors, pins)
        _nudge.apply_nudge(mesh, assignment)
        _voxel.fill_unassigned(mesh)
        what = "excluded from" if self.mode == "EXCLUDE" else f"pinned to {self.bone}"
        self.report(
            {"INFO"},
            f"Nudged {len(selected)} verts {what} ({len(pins)} pins stored)",
        )
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_nudge)
    bpy.types.WindowManager.rigforge_nudge_bone = StringProperty(
        name="Bone",
        description="Nudge target bone (vertex-group name)",
        default="",
    )
    bpy.types.WindowManager.rigforge_nudge_value = FloatProperty(
        name="Value",
        description="Nudge pin weight",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )


def unregister():
    del bpy.types.WindowManager.rigforge_nudge_value
    del bpy.types.WindowManager.rigforge_nudge_bone
    bpy.utils.unregister_class(WM_OT_rigforge_nudge)
