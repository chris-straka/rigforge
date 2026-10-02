# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Metarig diagnostics (fail early with bone names + fixes).

Generate's raw failures are cryptic (an unknown rig type throws a
bare KeyError of the type string with no bone; a parented root throws
without saying which bone). diagnose_metarig runs the cheap checks
first and names the bone plus the fix for each issue. Read-only:
never touches the armature.
"""

import difflib

import bpy

from .. import rig_lists
from ..utils.naming import ROOT_NAME
from ..utils.rig import get_rigforge_type

# Blender clamps edit bones to ~2e-6 m on mode exit, so a bone this
# short is degenerate intent, never a real bone (smallest legit
# metarig bones are millimeters).
DEGENERATE_LENGTH = 1e-5


def _valid_types():
    if not rig_lists.rigs:
        rig_lists.get_internal_rigs()
    return rig_lists.rigs


def _has_typed_ancestor(metarig, bone):
    parent = bone.parent
    while parent is not None:
        if get_rigforge_type(metarig.pose.bones[parent.name]) != "":
            return True
        parent = parent.parent
    return False


def diagnose_metarig(metarig):
    """Check a metarig; returns {"errors": [...], "warnings": [...]}.

    Each issue is {"bone", "code", "message", "hint"}. Bone order is
    deterministic; repeated calls return equal results.
    """
    errors = []
    warnings = []
    bones = metarig.data.bones
    if not any(get_rigforge_type(metarig.pose.bones[b.name]) != "" for b in bones):
        errors.append(
            {
                "bone": None,
                "code": "no_typed_bones",
                "message": "no bone has a Rig Type",
                "hint": "Assign rig types (Bone Properties > Rigforge Type) "
                "or Generate silently does nothing",
            }
        )
        return {"errors": errors, "warnings": warnings}
    valid = _valid_types()
    for bone in bones:
        pose_bone = metarig.pose.bones[bone.name]
        raw = pose_bone.rigforge_type or ""
        rig_type = get_rigforge_type(pose_bone)
        if raw != "" and raw != rig_type:
            warnings.append(
                {
                    "bone": bone.name,
                    "code": "spaced_type",
                    "message": f"Rig Type {raw!r} reads as {rig_type!r}",
                    "hint": "Spaces are stripped; write the type without them",
                }
            )
        if rig_type != "" and rig_type not in valid:
            guess = difflib.get_close_matches(rig_type, valid, n=1, cutoff=0.6)
            message = f"unknown Rig Type {rig_type!r}"
            if guess:
                message += f" (did you mean {guess[0]!r}?)"
            errors.append(
                {
                    "bone": bone.name,
                    "code": "unknown_type",
                    "message": message,
                    "hint": "Bone Properties > Rigforge Type; Generate would "
                    "throw a bare KeyError for this",
                }
            )
        if bone.name == ROOT_NAME:
            if bone.parent is not None:
                errors.append(
                    {
                        "bone": bone.name,
                        "code": "root_parented",
                        "message": "'root' must have no parent",
                        "hint": "Unparent it in Edit Mode (Alt+P > Clear Parent)",
                    }
                )
            if rig_type not in ("", "basic.raw_copy"):
                errors.append(
                    {
                        "bone": bone.name,
                        "code": "root_typed",
                        "message": f"'root' must have no rig (has {rig_type!r})",
                        "hint": "Clear its Rig Type (basic.raw_copy allowed)",
                    }
                )
        length = (bone.head - bone.tail).length
        if length < DEGENERATE_LENGTH:
            warnings.append(
                {
                    "bone": bone.name,
                    "code": "zero_length",
                    "message": f"near-zero length ({length:.1e} m)",
                    "hint": "Give it length in Edit Mode or remove it; rig "
                    "math divides by bone length",
                }
            )
        if (
            rig_type == ""
            and bone.name != ROOT_NAME
            and not _has_typed_ancestor(metarig, bone)
        ):
            warnings.append(
                {
                    "bone": bone.name,
                    "code": "untyped_ignored",
                    "message": "untyped with no rigged ancestor",
                    "hint": "Generate carries it as a dead ORG- bone (no "
                    "rig, no deform); assign a Rig Type, parent it under "
                    "a rigged bone, or delete it",
                }
            )
    return {"errors": errors, "warnings": warnings}


def format_issues(result, limit=3):
    """One-line summary plus the first few issues for reports."""
    errors = result["errors"]
    warnings = result["warnings"]
    lines = [f"{len(errors)} errors, {len(warnings)} warnings"]
    for issue in errors[:limit]:
        where = issue["bone"] or "<rig>"
        lines.append(f"{where}: {issue['message']} ({issue['hint']})")
    if len(errors) > limit:
        lines.append(f"... and {len(errors) - limit} more errors")
    return "; ".join(lines)


# noinspection PyPep8Naming
class WM_OT_rigforge_diagnose_metarig(bpy.types.Operator):
    bl_idname = "wm.rigforge_diagnose_metarig"
    bl_label = "Diagnose Metarig"
    bl_description = "Check the active metarig for structural problems"
    # REGISTER without UNDO: read-only, changes nothing.
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == "ARMATURE" and "rig_id" not in obj.data)

    def execute(self, context):
        metarig = context.active_object
        if metarig is None or metarig.type != "ARMATURE":
            self.report({"ERROR"}, "Active object must be an armature")
            return {"CANCELLED"}
        if "rig_id" in metarig.data:
            self.report({"ERROR"}, "This is a generated rig, not a metarig")
            return {"CANCELLED"}
        result = diagnose_metarig(metarig)
        for issue in result["errors"] + result["warnings"]:
            print(
                f"DIAGNOSE {issue['code']} [{issue['bone'] or '<rig>'}]: "
                f"{issue['message']} -- {issue['hint']}"
            )
        if result["errors"]:
            self.report({"ERROR"}, format_issues(result))
            return {"CANCELLED"}
        if result["warnings"]:
            self.report({"WARNING"}, format_issues(result))
        else:
            n_rigs = len(
                {
                    get_rigforge_type(pb)
                    for pb in metarig.pose.bones
                    if get_rigforge_type(pb) != ""
                }
            )
            self.report(
                {"INFO"},
                f"Metarig OK: {len(metarig.data.bones)} bones, {n_rigs} rig types",
            )
        return {"FINISHED"}


def register():
    bpy.utils.register_class(WM_OT_rigforge_diagnose_metarig)


def unregister():
    bpy.utils.unregister_class(WM_OT_rigforge_diagnose_metarig)
