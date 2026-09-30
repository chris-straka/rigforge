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

## Refactor / codebase health backlog

- [ ] Lint debt: `ruff check` shows ~266 remaining (was 786 before the
      format pass). Grind down file-by-file, never in bulk: F401 unused
      imports (some are Blender registration side effects — verify each),
      UP031 `%`-formatting, B905 `zip(strict=)` (adding strict can raise),
      B007 unused loop vars. RUF012 is ignored repo-wide (Blender
      requires class-level `bl_options` sets).
- [ ] Weight-paint assistance: smoothing, mirror, and cleanup operators.
      Biggest artist-time sink; Rigify barely helps. Highest value item.
- [ ] Error diagnostics: Rigify failures on malformed metarigs are
      cryptic. Fail early with bone names + what to fix.
- [ ] Game-export depth: validation + per-profile settings + Godot
      import checks on top of `wm.rigforge_game_export`.
- [ ] Optional: rename `RIGIFY-` fcurve data-path prefix in
      `generate.py` (kept for generated-file compat; harmless either way).
- [ ] Optional: upstream watch — Blender keeps fixing Rigify; review
      new upstream commits periodically and hand-port (re-apply by
      judgment, never `git merge`: this tree has structurally diverged).
