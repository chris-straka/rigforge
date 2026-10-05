# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""One-click metarig auto-placement (thin UI over the headless fitter).

Select the subject mesh object(s), keep the HLL metarig active, run
wm.rigforge_auto_place: the same detect -> fit -> gate pipeline as
tools/auto_rig.py runs in-session (rigforge.auto_place, shared code —
UI and headless fits cannot drift apart) and the metarig is solved
onto the mesh in place. A failed gate restores the metarig untouched
(preset as-is + report rung) and names the failing bones.
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..auto_place import detect_landmarks as _detect
from ..auto_place import fit_metarig as _fit
from ..auto_place import make_hll_presets as _presets
from ..auto_place import unirig_hints as _hints
from ..auto_place import validate_fit as _gate
from ..utils.bones import align_bone_z_axis

SLICES = 120  # same default as the headless detector CLI

# Driver joints the stock presets float by design — mirrors HERO_OPEN /
# STALKER_OPEN in tools/auto_rig.py (that file is headless-only, so the
# tuples live here too rather than importing across the boundary).
HERO_OPEN = (
    "shoulder.L",
    "shoulder.R",
    "spine.004",
    "thigh.L",
    "thigh.R",
    "upper_arm.L",
    "upper_arm.R",
)
STALKER_OPEN = (
    "neck.001",
    "pelvis.L",
    "pelvis.R",
    "shoulder.L",
    "shoulder.R",
    "tail.001",
    "thigh.L",
    "thigh.R",
    "upper_arm.L",
    "upper_arm.R",
)


def _missing_bones(meta, kind):
    """Bones the solve indexes that the metarig lacks (exact, else []).

    Direct names come from the same tuples the solver uses; .R twins
    come from the live subtrees, so custom bones under a mirror root
    are required exactly when mirror_subtree would fail on them.
    """
    bones = meta.data.bones
    if kind == "hll_stalker":
        want = {
            *_fit.STALKER_SPINE,
            *_fit.STALKER_NECK,
            *_fit.STALKER_FRONT_LEG,
            *_fit.STALKER_REAR_LEG,
            *_fit.STALKER_TAIL,
            "shoulder.L",
            "pelvis.L",
            "breast.L",
            "breast.R",
        }
        mirror_roots = ("shoulder.L", "pelvis.L")
    else:
        want = {
            *_fit.DRIVERS,
            "shoulder.L",
            "hand.L",
            "foot.L",
            "toe.L",
            "thigh.L",
            "spine",
            "spine.001",
            "spine.002",
            "spine.003",
            "spine.004",
            "spine.005",
            "spine.006",
            "face",
        }
        mirror_roots = ("shoulder.L", "thigh.L")
    for root in mirror_roots:
        if root not in bones:
            continue
        for name in _presets.descendants(bones, root):
            if name.endswith(".L"):
                want.add(name[:-2] + ".R")
    return sorted(n for n in want if n not in bones)


def _join_subject(context, meshes):
    """Duplicate + join meshes into one baked subject (caller deletes it).

    Same semantics as the headless import_mesh (per-mesh transform
    apply, then join) but the artist's meshes are never touched.
    """
    dupes = []
    for mesh in meshes:
        dup = mesh.copy()
        dup.data = mesh.data.copy()
        dup.name = mesh.name + "_autoplace_src"
        context.scene.collection.objects.link(dup)
        dupes.append(dup)
    bpy.ops.object.mode_set(mode="OBJECT")
    for dup in dupes:
        bpy.ops.object.select_all(action="DESELECT")
        dup.select_set(True)
        context.view_layer.objects.active = dup
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if len(dupes) == 1:
        # As in import_mesh: joining a single object warns and clears
        # the active object, so skip the join.
        dupes[0].name = "rigforge_autoplace_subject"
        return dupes[0]
    bpy.ops.object.select_all(action="DESELECT")
    for dup in dupes:
        dup.select_set(True)
    context.view_layer.objects.active = dupes[0]
    bpy.ops.object.join()
    subject = context.view_layer.objects.active
    subject.name = "rigforge_autoplace_subject"
    return subject


def _snapshot_bones(meta):
    context = bpy.context
    context.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode="EDIT")
    snap = {
        b.name: (b.head.copy(), b.tail.copy(), b.roll, b.use_connect)
        for b in meta.data.edit_bones
    }
    bpy.ops.object.mode_set(mode="OBJECT")
    return snap


def _restore_bones(meta, snap):
    context = bpy.context
    context.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode="EDIT")
    for name, (head, tail, roll, connect) in snap.items():
        bone = meta.data.edit_bones.get(name)
        if bone is None:
            continue
        bone.head, bone.tail, bone.roll = head, tail, roll
        bone.use_connect = connect
    bpy.ops.object.mode_set(mode="OBJECT")


def _detect_landmarks(env, kind, facing):
    """Slice analysis + landmark detection (mirrors the detector CLI)."""
    if kind == "hll_stalker":
        long_sig, ymin, ymax = _detect.slice_signature_axis(env, SLICES, "y")
        trans_sig, zmin, zmax = _detect.slice_signature_axis(env, SLICES, "z")
        xs = [v.co.x for v in env.data.vertices]
        bounds = (min(xs), max(xs), ymin, ymax, zmin, zmax)
        landmarks = _detect.detect_quadruped(long_sig, trans_sig, bounds, facing)
        signature, _, _ = _detect.slice_signature(env, SLICES)
        return landmarks, signature
    signature, zmin, zmax = _detect.slice_signature(env, SLICES)
    landmarks = _detect.detect_biped(signature, zmin, zmax, facing)
    landmarks["kind"] = "biped"
    return landmarks, signature


def _fired_warns(warns):
    """Dotted paths of warn-status gate reports (any nesting depth)."""
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("status") == "warn" and path:
                out.append(path)
            for key, value in node.items():
                if key == "status":
                    continue
                walk(value, f"{path}.{key}" if path else str(key))

    walk(warns, "")
    return list(dict.fromkeys(out))


def _remove_temps(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.meshes.remove(data)
        except ReferenceError:
            pass


# noinspection PyPep8Naming
class WM_OT_rigforge_auto_place(bpy.types.Operator):
    bl_idname = "wm.rigforge_auto_place"
    bl_label = "Auto-Place Metarig to Mesh"
    bl_description = (
        "Fit the active HLL metarig to the selected mesh(es) with the "
        "deterministic landmark fitter (same solve as the headless tools)"
    )
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        description="Metarig class to fit (Auto detects tail.001 = stalker)",
        items=[
            ("AUTO", "Auto", "Detect from the metarig bones"),
            ("HLL_HERO", "HLL Hero", "Stylized biped metarig"),
            ("HLL_STALKER", "HLL Stalker", "Quadruped metarig"),
        ],
        default="AUTO",
    )
    facing: EnumProperty(
        name="Facing",
        description="Which way the subject faces (same default as headless)",
        items=[
            ("-Y", "-Y", "Faces Blender -Y (HLL heroes)"),
            ("+Y", "+Y", "Faces Blender +Y (HLL creatures)"),
        ],
        default="-Y",
    )
    validate: BoolProperty(
        name="Validation Gate",
        description="Fail (restoring the metarig) unless the fitted metarig "
        "passes closure / inside-margin / symmetry",
        default=True,
    )
    hints: StringProperty(
        name="Hints",
        description="Optional skintokens-joints/1 (or legacy unirig-joints/1) JSON "
        "consumed as soft priors "
        "(stalker only, same arbitration as fit --hints)",
        subtype="FILE_PATH",
        default="",
    )
    hints_rotated: BoolProperty(
        name="Hints Rotated",
        description="Apply the Y-up to Z-up rotation to hint coords "
        "(same as fit --hints-rotated)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == "ARMATURE")

    def execute(self, context):
        meta = context.active_object
        if meta is None or meta.type != "ARMATURE":
            self.report({"ERROR"}, "Active object must be the HLL metarig")
            return {"CANCELLED"}
        if "rig_id" in meta.data:
            self.report(
                {"ERROR"},
                "Active armature is a generated rig, not a metarig: "
                "auto-place needs an hll_hero/hll_stalker metarig",
            )
            return {"CANCELLED"}
        meshes = [o for o in context.selected_objects if o.type == "MESH" and o != meta]
        if not meshes:
            self.report(
                {"ERROR"},
                "Select the subject mesh object(s) first "
                "(active object stays the metarig)",
            )
            return {"CANCELLED"}

        is_stalker = "tail.001" in meta.data.bones
        kind = "hll_stalker" if is_stalker else "hll_hero"
        if self.preset != "AUTO":
            want = "hll_stalker" if self.preset == "HLL_STALKER" else "hll_hero"
            if want != kind:
                self.report(
                    {"ERROR"},
                    f"metarig {'has' if is_stalker else 'lacks'} tail.001 "
                    f"but Preset={want}: set Preset to Auto or add the "
                    "matching HLL metarig",
                )
                return {"CANCELLED"}
        missing = _missing_bones(meta, kind)
        if missing:
            shown = ", ".join(missing[:8])
            self.report(
                {"ERROR"},
                f"metarig lacks {len(missing)} {kind} bones ({shown}): "
                "add a fresh hll_hero/hll_stalker metarig",
            )
            return {"CANCELLED"}
        if self.hints and kind != "hll_stalker":
            self.report({"ERROR"}, "Hints apply to hll_stalker only")
            return {"CANCELLED"}

        prev_selected = list(context.selected_objects)
        prev_active = context.view_layer.objects.active
        subject = env = None
        snap = _snapshot_bones(meta)
        try:
            subject = _join_subject(context, meshes)
            # Detect on a throwaway envelope (same stage split as
            # headless: the detector and the fitter each remesh).
            probe = _detect.envelope_copy(subject)
            landmarks, probe_sig = _detect_landmarks(probe, kind, self.facing)
            _remove_temps(probe)
            # Removing the probe clears the active object; the ops below
            # need one.
            context.view_layer.objects.active = subject
            if kind == "hll_stalker":
                targets = _fit.stalker_targets(landmarks)
                hints_report = None
                if self.hints:
                    hints_report, overrides = _fit.hint_target_overrides(
                        _hints, landmarks, self.hints, self.hints_rotated
                    )
                    for role, pos in overrides.items():
                        if role in targets:
                            targets[role] = pos
                env = _detect.envelope_copy(subject)
                signature = probe_sig
                cautions = []
                context.view_layer.objects.active = meta
                fit = _fit.fit_stalker_object(
                    _presets, _gate, meta, landmarks, env, targets, cautions
                )
            else:
                axes = _fit.track_axes_from_subject(_detect, subject, landmarks)
                env = axes["env"]
                signature = axes["signature"]
                context.view_layer.objects.active = meta
                fit = _fit.fit_hero(
                    _presets, align_bone_z_axis, meta, landmarks, axes, _gate
                )
                cautions = fit["cautions"]
                hints_report = None
            gate = None
            if self.validate:
                gate = _gate.run_gate(
                    env,
                    meta,
                    landmarks,
                    signature,
                    _gate.MARGIN_DEFAULT,
                    HERO_OPEN if kind == "hll_hero" else STALKER_OPEN,
                )
                if gate["verdict"] != "pass":
                    _restore_bones(meta, snap)
                    _remove_temps(subject, env)
                    print(f"gate FAIL: {len(gate['failures'])} failing check(s)")
                    for line in gate["failures"]:
                        print("STOP:", line)
                    self.report(
                        {"ERROR"},
                        "Auto-place gate FAIL "
                        f"({len(gate['failures'])} checks, metarig left as-is): "
                        f"{gate['failures'][0]}",
                    )
                    return {"CANCELLED"}
        except (SystemExit, KeyError, ValueError) as exc:
            _restore_bones(meta, snap)
            _remove_temps(subject, env)
            self.report({"ERROR"}, f"Auto-place failed, metarig left as-is: {exc}")
            return {"CANCELLED"}
        except Exception:
            _restore_bones(meta, snap)
            _remove_temps(subject, env)
            raise
        else:
            worst_after = max(joint["after_d"] for joint in fit["joints"].values())
            summary = (
                f"Auto-placed {kind}: {len(fit['joints'])} joints, "
                f"worst fit-delta {worst_after * 100:.1f} cm"
            )
            if self.validate:
                summary += (
                    f", gate PASS ({gate['inside']['checked']} drivers, "
                    f"min depth {gate['inside']['min_depth'] * 1000:.1f} mm)"
                )
                fired = _fired_warns(gate["warns"])
                if fired:
                    summary += f"; warns: {', '.join(fired)}"
            if cautions:
                summary += f"; {len(cautions)} caution(s), see console"
                for note in cautions:
                    print("CAUTION:", note)
            if hints_report is not None:
                summary += (
                    f"; hints {len(hints_report['trusted'])} trusted, "
                    f"{len(hints_report['diverged'])} measurement-wins"
                )
            self.report({"INFO"}, summary)
            _remove_temps(subject, env)
            return {"FINISHED"}
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
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


def register():
    bpy.utils.register_class(WM_OT_rigforge_auto_place)


def unregister():
    bpy.utils.unregister_class(WM_OT_rigforge_auto_place)
