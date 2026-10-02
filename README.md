# rigforge

Fork of Blender's Rigify (0.6.10, copied from the Blender 5.2 app bundle)
aimed at game-ready rigs: HLL (Godot 4.7) characters and creatures, rigged
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
  --factory-startup --python /tmp/rigforge_smoke.py
```

Headless tests (`tests/`, same overlay + Blender invocation, e.g.
`--python tests/test_game_export.py`).

Game export: with a generated rig active, Armature properties >
Rigforge Game Export > Export Game GLB — writes a deform-bones-only
GLB (Godot-friendly settings) with all meshes bound via Armature
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

License: GPL-2.0-or-later (see `COPYING`). Fork of Blender's Rigify;
like all Blender addons this tree is GPL.
