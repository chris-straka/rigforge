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
- [x] Bake-off vs ../unirig-mac on one hero + one creature (joints,
      weights, tweak time, Godot import). Done 2026-09-30, results in
      docs/bakeoff/final.md: rigforge won hero and creature quality,
      unirig won only zero-touch time; Mixamo added as baseline.
- [x] Auto-placement research: deterministic landmark detector +
      metarig fitter + validation gate for hero and quadruped, unirig
      joints as optional hints (docs/auto_placement.md, phases 1-4.5).
- [x] `wm.rigforge_auto_place` operator + panel button (thin UI over
      the headless fitter; docs/auto_placement.md section 11,
      tests/test_auto_place.py)
- [x] Re-run the bake-off comparison with the fitter in the loop
      (docs/bakeoff/rerun.md: fit-delta 0.0 on both subjects,
      11.6 s / 9.3 s wall, zero touch — under unirig's ~7 min)

- [ ] Fork-or-add-on audit (owner request 2026-10-01): diff `rigforge/`
      against stock Rigify 0.6.10 from the Blender 5.2 bundle, excluding
      the module rename. List every change inside Rigify's own files vs
      code that only adds new files/operators on top. If rigforge only
      adds on top, propose turning it into a companion add-on that uses
      Blender's stock Rigify (Blender updates then arrive for free); if
      it edits internals, list which edits and whether each could move
      out. Report only; do not restructure without the owner's go-ahead.
- [ ] Mobile bone budget: the hero rig exports 160 deform bones. Add a
      lighter game-export profile for mobile (drop or merge face/twist
      bones) from the same rig, and measure skinning cost in Godot.
- [ ] Public-repo follow-ups: smart paths where cheap (BLENDER_BIN
      env override with this machine's Blender as default,
      skip-if-missing for external-asset suites); this rig stays the
      target, no portability crusade.

## Refactor / codebase health backlog

- [ ] Lint debt: `ruff check` shows ~266 remaining (was 786 before the
      format pass). Grind down file-by-file, never in bulk: F401 unused
      imports (some are Blender registration side effects — verify each),
      UP031 `%`-formatting, B905 `zip(strict=)` (adding strict can raise),
      B007 unused loop vars. RUF012 is ignored repo-wide (Blender
      requires class-level `bl_options` sets).
- [ ] Weight-paint assistance: smoothing, mirror, and cleanup operators.
      Biggest artist-time sink; Rigify barely helps. Highest value item.
      (W1 shipped 2026-10-01: voxel auto-weights, tube + robustness
      green; W1b shipped 2026-10-02: best-safe defaults, bends
      near-parity, shoulder/neck tracked (method ceiling); W2 shipped
      2026-10-02: nudge strokes, solver + operator green; W3 shipped
      2026-10-02: piece transfer with inpainting, solver + operator
      green; W4 shipped 2026-10-02: cleanup ops, rows + operator
      green (weight assist complete).)
- [ ] Error diagnostics: Rigify failures on malformed metarigs are
      cryptic. Fail early with bone names + what to fix.
- [ ] Game-export depth: validation + per-profile settings + Godot
      import checks on top of `wm.rigforge_game_export`.
- [ ] Optional: rename `RIGIFY-` fcurve data-path prefix in
      `generate.py` (kept for generated-file compat; harmless either way).
- [ ] Optional: upstream watch — Blender keeps fixing Rigify; review
      new upstream commits periodically and hand-port (re-apply by
      judgment, never `git merge`: this tree has structurally diverged).
