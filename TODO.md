# rigforge TODO

- [x] Day 0: pristine 0.6.10 copy, headless register + metarig + generate
- [x] Rename pass: module rigify -> rigforge (ids + generated imports),
      coexists with bundled Rigify (smoke loads our copy, generates 706 bones)
- [x] One-click game export: deform-bones-only GLB with Godot settings
      (`wm.rigforge_game_export`, Armature panel button, tests/test_game_export.py)
- [x] HLL presets: stylized biped + creature metarig starting points
      (hll_hero + hll_stalker, built by tools/make_hll_presets.py,
      covered by tests/test_presets.py; baked-anim export in
      tests/test_game_export.py)
- [ ] Bake-off vs ../unirig-mac on one hero + one creature (joints,
      weights, tweak time, Godot import)
- [ ] Auto-placement research (winner of the bake-off decides the base)
