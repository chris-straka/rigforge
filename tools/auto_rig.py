# SPDX-License-Identifier: GPL-2.0-or-later
"""One-command auto-rig wrapper: triage-routed mesh in, rigged game GLB out.

STABLE CLI CONTRACT (an external eval harness shells out to this — keep it):

    rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
    BLENDER_USER_SCRIPTS=/tmp/rf_scripts \\
      /Applications/Blender.app/Contents/MacOS/Blender --background \\
      --factory-startup --python tools/auto_rig.py -- \\
      MESH --preset hll_hero|hll_stalker --out RIGGED.glb [--blend FILE]
      [--no-validate]

Arguments:

- MESH: subject mesh file (glb/gltf/fbx/obj — whatever
  tools/detect_landmarks.py imports). Read-only; never modified.
- --preset: metarig preset. hll_hero runs the biped pipeline
  (transverse slices); hll_stalker runs the quadruped pipeline
  (longitudinal spine analysis + legs from below; see
  docs/auto_placement.md). Each preset routes its own detector kind,
  fitter preset, and gate driver set.
- --out RIGGED.glb: written on success (parent dirs created).
- --blend FILE: also save the final session (mesh + metarig + rig) here.
- --no-validate: skip the validation gate (NOT recommended — the gate
  is the fail-stop that keeps bad fits from shipping).
- --hints FILE: skintokens-joints/1 (or legacy unirig-joints/1) JSON consumed by the fit stage as soft
  priors (agree -> trust, disagree -> measurement wins + divergence
  report; see tools/unirig_hints.py). --hints-rotated applies the Y-up
  -> Z-up rotation to the hint coords (for normalized subjects).

Pipeline stages (in order):

    landmarks -> fit -> validate (gate, fail-stop) -> generate
        -> bind (auto-weights) -> export

Bind fallback (same stage): bone-heat auto-weights is primary. If heat
finds no solution (production meshes with stacked shells fail it with
empty groups), bind retries heat on the single-shell envelope remesh and
data-transfers the weights onto the subject (nearest-face interpolated).
If both are empty the stage fails clean naming the mesh. After either
rung, verts left with no usable weight copy the nearest weighted vert
(the glTF exporter would otherwise park them on a synthesized
non-DEF neutral_bone joint).

Orientation (part of the landmarks stage): generator meshes arrive Y-up
(height along +Y) while the chain expects Z-up. A Y-up subject is
rotated +90 deg about X and run from a normalized copy in the workdir
(the input file is never modified); Z-up inputs pass through untouched.
For hll_stalker the Y-up test additionally requires the Y span to start
at the ground: a Z-up quadruped's longest span is its body length
(ymin well below zero), which must not trigger the rotation.

Exit contract:

- exit 0 with the GLB written on success; stdout ends with
  RIGFORGE_AUTO_RIG_OK.
- exit 1 on ANY failure; stdout ends with a single verdict line
  RIGFORGE_AUTO_RIG_FAIL stage=<stage> reason=<one line> naming the
  failing stage and why (tracebacks go to stderr only — never the
  verdict). stage=usage covers CLI/environment errors before the
  first stage (bad args, missing mesh file, missing addon overlay).

Reuse: landmarks/fit/validate run the existing tools/ scripts
in-session (same runpy pattern as tests/test_auto_placement.py);
generate/bind/export drive the real addon paths (pose.rigforge_generate,
ARMATURE_AUTO parent, wm.rigforge_game_export). No tool internals are
reimplemented here — only stage wiring, session-object lookup, and the
exit contract.
"""

import contextlib
import importlib.util
import io
import json
import math
import os
import runpy
import sys
import tempfile
import traceback

import bpy
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree

TOOLS = os.path.dirname(os.path.abspath(__file__))

# Driver joints the stock presets float by design (socket/hip/neck pivots
# with real gaps) — explicitly listed as intentionally open, same as
# tests/test_auto_placement.py.
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

PRESETS = ("hll_hero", "hll_stalker")

STAGES_OK = "RIGFORGE_AUTO_RIG_OK"
STAGES_FAIL = "RIGFORGE_AUTO_RIG_FAIL"


class StageFail(Exception):
    """A named pipeline-stage failure (the harness verdict, not a crash)."""

    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def run_tool(filename, argv):
    """Run a tools/ script in-session with patched argv; echo + capture."""
    old_argv = sys.argv
    buf = io.StringIO()
    code = None
    sys.argv = ["auto_rig", "--", *argv]
    try:
        with contextlib.redirect_stdout(buf):
            runpy.run_path(os.path.join(TOOLS, filename), run_name="__main__")
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old_argv
    out = buf.getvalue()
    sys.stdout.write(out)
    sys.stdout.flush()
    return out, code


def load_tool(name, filename):
    path = os.path.join(TOOLS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def normalize_orientation(mesh_path, workdir, preset):
    """Y-up subjects -> a Z-up normalized copy; Z-up passes through.

    Part of the landmarks stage: generator meshes arrive with height
    along +Y while slice analysis expects Z-up. Detection: height is a
    character's longest extent, so Y-span > X- and Z-span means Y-up.
    The +90 deg X rotation maps +Y to +Z and keeps feet (y=0) at z=0.
    Quadrupeds additionally require ymin at the ground: a Z-up
    quadruped's longest span is body length (ymin < 0), not height.
    """
    dl = load_tool("rf_auto_rig_dl", "detect_landmarks.py")
    try:
        obj = dl.import_mesh(mesh_path)
    except SystemExit as e:
        raise StageFail("landmarks", one_line(str(e.code or e))) from None
    except Exception as e:
        raise StageFail("landmarks", one_line(str(e) or type(e).__name__)) from None
    corner = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    spans = [max(c[i] for c in corner) - min(c[i] for c in corner) for i in range(3)]
    y_up = spans[1] > spans[0] and spans[1] > spans[2]
    if y_up and preset == "hll_stalker":
        ymin = min(c[1] for c in corner)
        y_up = abs(ymin) < 0.05 * spans[1]
    if not y_up:
        print(f"[auto_rig] orientation: Z-up, using input as-is (spans={spans})")
        return mesh_path
    # Bake into mesh data directly (no object-level rotate/apply round trip).
    obj.data.transform(Matrix.Rotation(math.radians(90.0), 4, "X"))
    obj.data.update()
    bpy.context.view_layer.update()
    corner = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    check = [max(c[i] for c in corner) - min(c[i] for c in corner) for i in range(3)]
    if not (check[2] > check[0] and check[2] > check[1]):
        raise StageFail("landmarks", f"orientation normalization failed: {check}")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    copy_path = os.path.join(workdir, "subject_zup.glb")
    bpy.ops.export_scene.gltf(
        filepath=copy_path, export_format="GLB", use_selection=True
    )
    print(f"[auto_rig] orientation: Y-up -> normalized copy {copy_path}")
    return copy_path


def payload_between(output, begin, end, stage, what):
    try:
        return output.split(begin)[1].split(end)[0]
    except IndexError:
        raise StageFail(stage, f"{what}: markers {begin}/{end} missing") from None


def stage_landmarks(mesh_path, preset, workdir):
    """Landmarks via tools/detect_landmarks.py; returns the landmarks dict."""
    kind = "quadruped" if preset == "hll_stalker" else "biped"
    out, code = run_tool("detect_landmarks.py", [mesh_path, "--kind", kind])
    if code is not None:
        raise StageFail("landmarks", one_line(str(code) or "detector failed"))
    raw = payload_between(
        out,
        "RIGFORGE_LANDMARKS_BEGIN",
        "RIGFORGE_LANDMARKS_OK",
        "landmarks",
        "landmark report",
    )
    try:
        landmarks = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StageFail("landmarks", f"landmark JSON unparseable: {e}") from e
    lm_path = os.path.join(workdir, "landmarks.json")
    with open(lm_path, "w", encoding="utf-8") as f:
        json.dump(landmarks, f, indent=2)
    print(f"[auto_rig] landmarks OK: {lm_path}")
    return landmarks, lm_path


def stage_fit(mesh_path, lm_path, workdir, preset, hints, hints_rotated):
    """Fit via tools/fit_metarig.py (generate deferred to its own stage)."""
    fitted = os.path.join(workdir, "fitted_metarig.py")
    argv = [
        mesh_path,
        lm_path,
        "--out",
        fitted,
        "--no-generate",
        "--no-blend",
        "--preset",
        preset,
    ]
    if hints:
        argv += ["--hints", hints]
    if hints_rotated:
        argv.append("--hints-rotated")
    out, code = run_tool("fit_metarig.py", argv)
    if code is not None:
        raise StageFail("fit", one_line(str(code) or "fitter failed"))
    payload_between(out, "RIGFORGE_FIT_BEGIN", "RIGFORGE_FIT_OK", "fit", "fit report")
    if not os.path.exists(fitted):
        raise StageFail("fit", "fitted metarig module not emitted")
    print(f"[auto_rig] fit OK: {fitted}")
    return fitted


def stage_validate(mesh_path, fitted, lm_path, workdir, preset):
    """Gate via tools/validate_fit.py (fail-stop; names bones and fixes)."""
    report_path = os.path.join(workdir, "validate_report.json")
    allow = STALKER_OPEN if preset == "hll_stalker" else HERO_OPEN
    out, code = run_tool(
        "validate_fit.py",
        [
            mesh_path,
            fitted,
            lm_path,
            "--open",
            ",".join(allow),
            "--no-render",
            "--report",
            report_path,
        ],
    )
    if code is not None or "RIGFORGE_VALIDATE_OK" not in out:
        stops = [ln for ln in out.splitlines() if ln.startswith("STOP:")]
        last = (out.splitlines() or ["gate produced no output"])[-1]
        detail = " | ".join(stops[:3]) if stops else one_line(last)
        raise StageFail("validate", f"gate FAIL: {detail}")
    print(f"[auto_rig] validate OK: {report_path}")
    return report_path


def find_metarig():
    """The fitted metarig: an armature without rig_id (metarig bones carry
    rigify_type, which a subject-imported armature never has)."""
    armatures = [
        o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" not in o.data
    ]
    if not armatures:
        raise StageFail("generate", "no metarig in session (fit/validate lost it?)")
    typed = [o for o in armatures if any("rigify_type" in b for b in o.data.bones)]
    if len(typed) == 1:
        return typed[0]
    if len(armatures) == 1:
        return armatures[0]
    names = sorted(o.name for o in armatures)
    raise StageFail("generate", f"ambiguous metarig candidates: {names}")


def stage_generate():
    """Generate the control rig from the fitted metarig; returns the rig."""
    meta = find_metarig()
    rigs_before = {
        o.name for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data
    }
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    meta.select_set(True)
    bpy.context.view_layer.objects.active = meta
    try:
        bpy.ops.pose.rigforge_generate()
    except RuntimeError as e:
        raise StageFail("generate", f"rigforge_generate failed: {e}") from e
    rigs = [
        o
        for o in bpy.data.objects
        if o.type == "ARMATURE" and "rig_id" in o.data and o.name not in rigs_before
    ]
    if not rigs:
        rigs = [
            o for o in bpy.data.objects if o.type == "ARMATURE" and "rig_id" in o.data
        ]
    if len(rigs) != 1:
        raise StageFail(
            "generate", f"generate left {len(rigs)} rigs, expected exactly 1"
        )
    rig = rigs[0]
    n_def = sum(1 for b in rig.data.bones if b.name.startswith("DEF-"))
    print(f"[auto_rig] generate OK: {rig.name} ({n_def} DEF bones)")
    if n_def <= 30:
        raise StageFail("generate", f"only {n_def} DEF bones")
    return rig


def find_subject():
    """The joined subject mesh (not the voxel-remesh envelope copy).

    The envelope is a Blender duplicate of the joined import, so its name
    is the subject name plus a .001-style numeric suffix. Fallback: the
    subject keeps its import UVs while the voxel remesh strips them.
    """
    meshes = [
        o
        for o in bpy.data.objects
        if o.type == "MESH" and o.data.vertices and not o.name.startswith("WGT-")
    ]
    if not meshes:
        raise StageFail("bind", "no meshes in session (import lost?)")
    if len(meshes) == 1:
        return meshes[0]
    names = {o.name for o in meshes}
    bases = set()
    for o in meshes:
        stem, dot, suffix = o.name.rpartition(".")
        if dot and suffix.isdigit() and stem in names:
            bases.add(stem)
    if len(bases) == 1:
        return next(o for o in meshes if o.name == bases.pop())
    uvved = [o for o in meshes if len(o.data.uv_layers)]
    if len(uvved) == 1:
        return uvved[0]
    shown = sorted(o.name for o in meshes)[:5]
    raise StageFail("bind", f"ambiguous subject meshes ({len(meshes)}): {shown}...")


# Same floor the glTF exporter uses (min_influence): entries at or below
# this do not count as a joint assignment.
MIN_INFLUENCE = 0.0001


def count_usable(obj):
    return sum(
        1
        for v in obj.data.vertices
        if any(el.weight > MIN_INFLUENCE for el in v.groups)
    )


def fill_unassigned(obj):
    """Copy nearest-weighted-vert weights onto verts with no usable entry.

    Returns the number of verts filled. Without this, the exporter parks
    such verts on a synthesized non-DEF neutral_bone joint.
    """
    verts = obj.data.vertices
    good_idx = [
        v.index for v in verts if any(el.weight > MIN_INFLUENCE for el in v.groups)
    ]
    bad = [
        v.index for v in verts if not any(el.weight > MIN_INFLUENCE for el in v.groups)
    ]
    if not bad or not good_idx:
        return 0
    kd = KDTree(len(good_idx))
    for i, vi in enumerate(good_idx):
        kd.insert(verts[vi].co, i)
    kd.balance()
    groups = obj.vertex_groups
    for vi in bad:
        _, i, _ = kd.find(verts[vi].co)
        for el in verts[good_idx[i]].groups:
            if el.weight > MIN_INFLUENCE:
                groups[el.group].add([vi], el.weight, "REPLACE")
    return len(bad)


def clear_skin(obj):
    for mod in [m for m in obj.modifiers if m.type == "ARMATURE"]:
        obj.modifiers.remove(mod)
    obj.vertex_groups.clear()


def heat_bind(mesh_obj, rig):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh_obj.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")


def transfer_weights(src, dst):
    """Data-transfer src vertex groups onto dst (nearest-face interpolated).

    Destination groups must pre-exist (dst=NAME matches, never creates).
    """
    for group in src.vertex_groups:
        dst.vertex_groups.new(name=group.name)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    dst.select_set(True)
    bpy.context.view_layer.objects.active = dst
    dt = dst.modifiers.new("RF_Weights", "DATA_TRANSFER")
    dt.object = src
    dt.use_vert_data = True
    dt.data_types_verts = {"VGROUP_WEIGHTS"}
    dt.vert_mapping = "POLYINTERP_NEAREST"
    dt.layers_vgroup_select_src = "ALL"
    dt.layers_vgroup_select_dst = "NAME"
    dt.mix_mode = "REPLACE"
    bpy.ops.object.modifier_apply(modifier=dt.name)


def find_envelope_name(subject_name):
    """The envelope remesh paired with the subject (pre-generate session)."""
    rest = [
        o.name
        for o in bpy.data.objects
        if o.type == "MESH"
        and o.data.vertices
        and not o.name.startswith("WGT-")
        and o.name != subject_name
    ]
    return rest[0] if len(rest) == 1 else None


def stage_bind(rig, subject_name, envelope_name):
    """Bind the subject mesh: heat auto-weights, else envelope-transfer."""
    subject = bpy.data.objects.get(subject_name)
    if subject is None or subject.type != "MESH":
        raise StageFail("bind", f"subject mesh {subject_name!r} lost after generate")
    try:
        heat_bind(subject, rig)
    except RuntimeError as e:
        print(f"[auto_rig] bind: direct heat raised, trying envelope: {e}")
        clear_skin(subject)
    if count_usable(subject) > 0:
        check_bound(subject, rig)
        filled = fill_unassigned(subject)
        print(
            f"[auto_rig] bind OK: {subject.name} -> {rig.name} "
            f"(automatic weights, gap-filled {filled} verts)"
        )
        return subject
    print("[auto_rig] bind: direct heat empty, retrying via the envelope")
    envelope = bpy.data.objects.get(envelope_name) if envelope_name else None
    if envelope is None or envelope.type != "MESH":
        raise StageFail(
            "bind",
            f"{subject.name}: bone heat found no solution and no envelope "
            "remesh is available — mesh needs manual skinning",
        )
    clear_skin(subject)
    try:
        heat_bind(envelope, rig)
    except RuntimeError as e:
        clear_skin(envelope)
        envelope.parent = None
        raise StageFail("bind", f"envelope heat failed: {e}") from e
    if count_usable(envelope) == 0:
        clear_skin(envelope)
        envelope.parent = None
        raise StageFail(
            "bind",
            f"{subject.name}: bone heat empty on subject and envelope — "
            "mesh needs manual skinning",
        )
    transfer_weights(envelope, subject)
    clear_skin(envelope)
    envelope.parent = None
    mod = subject.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    if count_usable(subject) == 0:
        raise StageFail("bind", f"{subject.name}: weight transfer came back empty")
    check_bound(subject, rig)
    filled = fill_unassigned(subject)
    print(
        f"[auto_rig] bind OK: {subject.name} -> {rig.name} "
        "(heat via envelope transfer: direct heat found no solution; "
        f"gap-filled {filled} verts)"
    )
    return subject


def check_bound(subject, rig):
    bound = any(m.type == "ARMATURE" and m.object == rig for m in subject.modifiers)
    if not bound:
        raise StageFail("bind", f"{subject.name} has no Armature modifier on the rig")


def stage_export(rig, out_path, blend_path):
    """Game-GLB export of the rig + bound meshes via wm.rigforge_game_export."""
    out_abs = os.path.abspath(out_path)
    parent = os.path.dirname(out_abs)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if blend_path:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path))
        print(f"[auto_rig] blend saved: {blend_path}")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    result = bpy.ops.wm.rigforge_game_export(filepath=out_abs)
    if result != {"FINISHED"}:
        raise StageFail("export", f"game export operator returned {result}")
    if not os.path.exists(out_abs) or os.path.getsize(out_abs) == 0:
        raise StageFail("export", f"export wrote no GLB at {out_abs}")
    print(f"[auto_rig] export OK: {out_abs} ({os.path.getsize(out_abs)} bytes)")


def one_line(text):
    return " ".join(text.split())


def parse_args(argv):
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    positional = []
    preset = out = blend = hints = None
    no_validate = hints_rotated = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--preset":
            i += 1
            preset = args[i] if i < len(args) else None
        elif arg == "--out":
            i += 1
            out = args[i] if i < len(args) else None
        elif arg == "--blend":
            i += 1
            blend = args[i] if i < len(args) else None
        elif arg == "--hints":
            i += 1
            hints = args[i] if i < len(args) else None
        elif arg == "--hints-rotated":
            hints_rotated = True
        elif arg == "--no-validate":
            no_validate = True
        elif arg.startswith("-"):
            raise StageFail("usage", f"unknown flag {arg}")
        else:
            positional.append(arg)
        i += 1
    if len(positional) != 1:
        raise StageFail(
            "usage", "usage: auto_rig.py MESH --preset hll_hero|hll_stalker --out FILE"
        )
    if preset not in PRESETS:
        raise StageFail("usage", f"--preset must be one of {PRESETS}, got {preset!r}")
    if not out:
        raise StageFail("usage", "missing required --out RIGGED.glb")
    mesh = positional[0]
    if not os.path.isfile(mesh):
        raise StageFail("usage", f"mesh not found: {mesh}")
    if hints is not None and not os.path.isfile(hints):
        raise StageFail("usage", f"hints not found: {hints}")
    return mesh, preset, out, blend, no_validate, hints, hints_rotated


def main():
    mesh_path, preset, out_path, blend_path, no_validate, hints, hints_rotated = (
        parse_args(sys.argv)
    )
    try:
        bpy.ops.preferences.addon_enable(module="rigforge")
    except RuntimeError:
        raise StageFail(
            "usage", "rigforge addon not found; set BLENDER_USER_SCRIPTS overlay"
        ) from None
    workdir = tempfile.mkdtemp(prefix="rigforge_auto_")
    print(f"[auto_rig] preset={preset} mesh={mesh_path} workdir={workdir}")
    stages = ["landmarks", "fit"]
    mesh_path = normalize_orientation(mesh_path, workdir, preset)
    _landmarks, lm_path = stage_landmarks(mesh_path, preset, workdir)
    fitted = stage_fit(mesh_path, lm_path, workdir, preset, hints, hints_rotated)
    if no_validate:
        print("[auto_rig] validate SKIPPED (--no-validate)")
    else:
        stage_validate(mesh_path, fitted, lm_path, workdir, preset)
        stages.append("validate")
    # Resolve the subject BEFORE generate: generate adds ~200 WGT-* widget
    # meshes to the session, and only the subject/envelope pair exists now.
    subject_name = find_subject().name
    envelope_name = find_envelope_name(subject_name)
    print(f"[auto_rig] subject: {subject_name} envelope: {envelope_name}")
    rig = stage_generate()
    stage_bind(rig, subject_name, envelope_name)
    stage_export(rig, out_path, blend_path)
    stages += ["generate", "bind", "export"]
    print(f"{STAGES_OK} out={os.path.abspath(out_path)} stages={','.join(stages)}")


if __name__ == "__main__":
    try:
        main()
    except StageFail as e:
        print(f"{STAGES_FAIL} stage={e.stage} reason={one_line(e.reason)}")
        raise SystemExit(1) from None
    except Exception as e:
        # Verdict stays clean on stdout; the traceback goes to stderr only.
        traceback.print_exc()
        print(
            f"{STAGES_FAIL} stage=unknown reason={one_line(str(e) or type(e).__name__)}"
        )
        raise SystemExit(1) from None
