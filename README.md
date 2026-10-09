# rigforge

Fork of Blender's Rigify (0.6.10, copied from the Blender 5.2 app bundle)
aimed at game-ready rigs: HLL (Bevy, Rust) characters and creatures, rigged
in Blender, exported clean.

- License: GPL-2.0-or-later (same as upstream; Blender addons must be GPL).
- Upstream: none vendored as a remote (source is the Blender install);
  re-copy from a newer Blender to rebase.
- Day-0 state: pristine copy + headless smoke test (register, add human
  metarig, generate). See TODO.md.

Layout: `rigforge/` is the addon. Zip that folder for Install-from-Disk.

Smoke test:

```sh
rsync -a --delete rigforge/ /tmp/rf_scripts/addons/rigforge/
BLENDER_USER_SCRIPTS=/tmp/rf_scripts \
  /Applications/Blender.app/Contents/MacOS/Blender --background \
  --factory-startup --python tests/smoke.py
```

Headless tests (`tests/`): run one via `tools/run_test.sh
tests/test_game_export.py` (syncs the addon overlay, then the same
Blender invocation). Binaries resolve via env with same-machine
defaults: `BLENDER_BIN` (default
`/Applications/Blender.app/Contents/MacOS/Blender`), `BEVY_GLB_CHECK`
(glbkit's headless Bevy loader check, default
`~/Games/_blender/rfcheck/glbkit/compat/bevy/target/release/examples/check`, used
by `tools/bevy_import_check.py`; skipped when not built). The Godot
import check was removed with the Godot version of HLL (2026-10-05).
Suites needing files outside the repo (e.g.
`test_auto_placement.py`'s HLL subjects) skip absent subjects
instead of failing.

No Blender install (cloud sessions, CI): `pip install bpy` (5.0.1,
CPython 3.11; Linux also needs `libegl1`), then
`python tools/run_test_bpy.py tests/test_cleanup.py` — same overlay,
in-process. The wheel trails the 5.2 this fork tracks, so the Mac
binary stays canonical; all suites pass on both as of 2026-10-03.
CI (`.github/workflows/tests.yml`) runs the smoke test + every suite
this way on each PR and push to main; new `tests/test_*.py` files
must be added to its matrix (a check fails otherwise).

Game export: with a generated rig active, Armature properties >
Rigforge Game Export > Export Game GLB — writes a deform-bones-only
GLB (game-engine settings, loads in Bevy) with all meshes bound via Armature
modifiers. Headless: `bpy.ops.wm.rigforge_game_export(filepath=...)`.

Presets: `hll_hero` (stylized biped, Andras-scale) and `hll_stalker`
(quadruped, stalker-scale) metarigs in Add > Armature > Rigify
Meta-Rigs. Generated modules — rebuild with
`--python tools/make_hll_presets.py -- .` after tuning constants.

Auto-place: with the HLL metarig active and the subject mesh(es)
selected, Armature properties > Rigforge > Auto-Place to Mesh fits
the metarig to the mesh in place (strict validation gate, metarig
restored untouched on failure). Headless: one command per subject,
`--python tools/auto_rig.py -- MESH --preset hll_hero|hll_stalker
--out RIGGED.glb` (see `docs/bakeoff/rerun.md` for numbers).

genforge adapter (repair-rig): genforge's `gen character` chain rerigs
a character whose skeleton or weights can't be saved with

```sh
tools/genforge_adapter.sh repair-rig IN.glb OUT.glb RESULT.json [--class humanoid|quadruped|custom]
```

It drops the input's armature (meshes, materials and textures stay),
runs `tools/auto_rig.py` with `hll_hero` (humanoid) or `hll_stalker`
(quadruped) and writes the 160-bone deform GLB plus a `result.json` in
genforge's adapter schema (`{"ok", "outputs", "tool": "rigforge", ...}`;
exit 0 rigged, 1 an auto-rig stage failed, 2 error with no result).
Custom skeletons have no preset (`ok: false`). genforge re-standardizes
the output with motionforge before rechecking. Test:
`tools/run_test.sh tests/test_genforge_adapter.py`. Set
`RIGFORGE_SCRIPTS` to use an overlay folder other than `/tmp/rf_scripts`.

License: GPL-2.0-or-later (see `COPYING`). Fork of Blender's Rigify;
like all Blender addons this tree is GPL.
