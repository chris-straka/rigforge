# SPDX-License-Identifier: GPL-2.0-or-later
"""genforge adapter: repair-rig (rerig a character whose skin can't be saved).

genforge's character chain (genforge docs/phase-7.md) calls this when its
check stage finds a broken skeleton, or when weightforge's fix gives up.
It wraps tools/auto_rig.py and speaks genforge's adapter contract:

    tools/genforge_adapter.sh repair-rig IN.glb OUT.glb RESULT.json \\
        [--class humanoid|quadruped|custom]

(the .sh wrapper syncs the addon overlay and runs this inside headless
Blender: `--python tools/genforge_adapter.py -- repair-rig ...`).

Steps: import IN.glb, drop its armature (meshes return to rest; old
bone vertex groups removed; materials and textures kept), export the
bare meshes to a temp GLB, run auto_rig.py on it with the class preset
(humanoid -> hll_hero, quadruped -> hll_stalker) and write OUT.glb.
genforge re-standardizes the result (motionforge) before rechecking it,
so the rerig lands on the same canonical skeleton as every character.

RESULT.json (same schema as the weightforge/motionforge adapters):
{"ok": bool, "outputs": [paths relative to RESULT.json's folder],
 "tool": "rigforge", "stage": "repair-rig", "repair-rig": {...},
 "reason"?: str}
Exit 0 = rigged; 1 = auto-rig failed a stage (ok false, result
written); 2 = error (bad args, unreadable input, addon missing): no
result file, so the chain stops.
"""

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile

import bpy

TOOLS = os.path.dirname(os.path.abspath(__file__))
PRESET_FOR_CLASS = {"humanoid": "hll_hero", "quadruped": "hll_stalker"}
OK_MARK = "RIGFORGE_AUTO_RIG_OK"
FAIL_MARK = "RIGFORGE_AUTO_RIG_FAIL"


class AdapterError(Exception):
    """Exit-2 errors: no result.json is written."""


def parse(argv):
    if len(argv) < 4:
        raise AdapterError(
            "usage: genforge_adapter repair-rig IN.glb OUT.glb RESULT.json "
            "[--class humanoid|quadruped|custom]"
        )
    stage, src, out, result = argv[:4]
    if stage != "repair-rig":
        raise AdapterError(f"unknown adapter stage {stage!r} (rigforge has repair-rig)")
    klass = "humanoid"
    rest = argv[4:]
    i = 0
    while i < len(rest):
        if rest[i] == "--class" and i + 1 < len(rest):
            klass = rest[i + 1]
            i += 2
        else:
            raise AdapterError(f"unknown flag {rest[i]!r}")
    if klass not in ("humanoid", "quadruped", "custom"):
        raise AdapterError(f"--class must be humanoid|quadruped|custom, got {klass!r}")
    if not os.path.isfile(src):
        raise AdapterError(f"input not found: {src}")
    return src, out, result, klass


def strip_rig(src, bare_path):
    """Import src, remove armatures/skin, export the bare meshes."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        # No bind-pose guessing: with it, meshes under a translated root
        # node come back lifted by that translation.
        bpy.ops.import_scene.gltf(filepath=src, guess_original_bind_pose=False)
    except RuntimeError as exc:
        raise AdapterError(f"cannot import {src}: {exc}") from None
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.visible_get()]
    if not meshes:
        raise AdapterError(f"{src} has no visible mesh")
    bones_before = 0
    for obj in list(bpy.data.objects):
        if obj.type == "ARMATURE":
            bones_before = max(bones_before, len(obj.data.bones))
    for obj in meshes:
        for mod in list(obj.modifiers):
            if mod.type == "ARMATURE":
                obj.modifiers.remove(mod)
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
        obj.vertex_groups.clear()
    for obj in list(bpy.data.objects):
        if obj.type != "MESH" or obj not in meshes:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.export_scene.gltf(
        filepath=bare_path,
        export_format="GLB",
        use_selection=True,
        export_animations=False,
        export_skins=False,
    )
    verts = sum(len(o.data.vertices) for o in meshes)
    return {"meshes": len(meshes), "vertices": verts, "bones_before": bones_before}


def run_auto_rig(mesh, preset, out):
    """auto_rig.py in-session; returns (verdict line, captured stdout)."""
    old_argv = sys.argv
    buf = io.StringIO()
    code = 0
    sys.argv = ["auto_rig", "--", mesh, "--preset", preset, "--out", out]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                runpy.run_path(os.path.join(TOOLS, "auto_rig.py"), run_name="__main__")
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    text = buf.getvalue()
    sys.stdout.write(text)
    verdict = next(
        (
            ln
            for ln in reversed(text.splitlines())
            if ln.startswith((OK_MARK, FAIL_MARK))
        ),
        f"{FAIL_MARK} stage=unknown reason=auto_rig exited {code} without a verdict",
    )
    return verdict, code


def verdict_fields(verdict):
    fields = {}
    rest = verdict.split(" ", 1)[1] if " " in verdict else ""
    if "reason=" in rest:
        head, reason = rest.split("reason=", 1)
        fields["reason"] = reason.strip()
        rest = head
    for part in rest.split():
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k] = v
    return fields


def relative(path, base):
    path = os.path.realpath(path)
    base = os.path.realpath(base)
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def write_result(result, payload):
    with open(result, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def run_adapter(argv):
    """Do the work in this Blender session. Returns (exit code, payload or None)."""
    try:
        src, out, result, klass = parse(argv)
    except AdapterError as exc:
        print(f"genforge_adapter: {exc}", file=sys.stderr)
        return 2, None
    base = os.path.dirname(os.path.abspath(result))
    preset = PRESET_FOR_CLASS.get(klass)
    if preset is None:
        payload = {
            "ok": False,
            "outputs": [],
            "tool": "rigforge",
            "stage": "repair-rig",
            "reason": "custom skeletons have no rigforge preset; rerig by hand",
            "repair-rig": {"class": klass},
        }
        write_result(result, payload)
        return 1, payload
    with tempfile.TemporaryDirectory(prefix="rigforge_genforge_") as tmp:
        bare = os.path.join(tmp, "bare.glb")
        try:
            info = strip_rig(src, bare)
        except AdapterError as exc:
            print(f"genforge_adapter: {exc}", file=sys.stderr)
            return 2, None
        # auto_rig.py expects the stock startup scene (as with
        # --factory-startup), not an empty one.
        bpy.ops.wm.read_factory_settings()
        verdict, _code = run_auto_rig(bare, preset, out)
    fields = verdict_fields(verdict)
    ok = (
        verdict.startswith(OK_MARK) and os.path.isfile(out) and os.path.getsize(out) > 0
    )
    if not ok and fields.get("stage") == "usage":
        print(
            f"genforge_adapter: auto_rig usage error: {fields.get('reason')}",
            file=sys.stderr,
        )
        return 2, None
    details = dict(
        info, preset=preset, **{k: v for k, v in fields.items() if k != "out"}
    )
    payload = {
        "ok": ok,
        "outputs": [relative(out, base)] if ok else [],
        "tool": "rigforge",
        "stage": "repair-rig",
        "repair-rig": details,
    }
    if not ok:
        payload["reason"] = (
            f"auto-rig failed at {fields.get('stage', '?')}: {fields.get('reason', '')}"
        )
    write_result(result, payload)
    return (0 if ok else 1), payload


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    code, _payload = run_adapter(args)
    sys.stdout.flush()
    sys.exit(code)
