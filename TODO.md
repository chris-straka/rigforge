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

- [x] Fork-or-add-on audit (docs/fork-audit.md, 2026-10-02):
      verdict is companion-feasible — zero functional edits inside
      Rigify's own files (renames + ruff autofixes proven mechanical,
      stock-vs-fork human rigs bit-identical at 706 bones / 198
      drivers / 220 widgets); only adds are 19 new files + 5 panels +
      6 submodule lines. One migration cost: renamed id-props in old
      .blends need a one-time adopt operator. Restructure pending
      owner's go-ahead (not started).
- [x] Mobile bone budget (2026-10-02): game-export MOBILE profile
      (Full/Mobile buttons) merges 95 face+twist DEF bones up to kept
      ancestors (face roots ride the topmost kept bone) on temp copies
      — 160 -> 65 joints, originals untouched, anims on kept joints.
      Both GLBs import clean in headless Godot 4.7.1 (160/65 bones).
      In-game FPS-side skinning measurement stays a game-side step
      (needs HLL scenes + profiler, hll repo).
- [x] Public-repo follow-ups (2026-10-02): smart paths where cheap
      (BLENDER_BIN env override with this machine's Blender as
      default, tools/run_test.sh runner, skip-if-missing for
      external-asset suites); this rig stays the target, no
      portability crusade.

## Refactor / codebase health backlog

- [x] Lint debt (2026-10-02): all rigforge-authored code clean
      under `ruff check` + `ruff format` (weights, operators,
      auto_place, metarigs, tools, tests). Remaining ~253 findings
      are pristine upstream-fork files, deliberately untouched:
      F401s there are re-exports (removal risk) and any rebase
      re-copy would wipe the churn. RUF012 ignored repo-wide
      (Blender requires class-level `bl_options` sets).
- [x] Weight-paint assistance (complete 2026-10-02): smoothing,
      mirror, and cleanup operators. (W1: voxel auto-weights, tube
      + robustness green; W1b: best-safe defaults, bends
      near-parity, shoulder/neck tracked (method ceiling); W2:
      nudge strokes, solver + operator green; W3: piece transfer
      with inpainting, solver + operator green; W4: cleanup ops,
      rows + operator green.)
- [x] Error diagnostics (2026-10-02): wm.rigforge_diagnose_metarig
      pre-flight (read-only, armature panel): unknown Rig Type per
      bone with did-you-mean (typos threw a bare KeyError), root
      parent/type rules, no-types rig, spaced-type / degenerate /
      stray warnings (strays proven to become dead ORG bones).
- [x] Game-export depth (2026-10-02): validate_export fails early
      (unweighted mesh, no DEF bones) + warns (partial weights,
      negative scale, mobile >4 influences); mobile cap-4 toggle
      (panel checkbox, auto-fix on copies); tools/godot_import_check
      (headless Godot 4.7.1 import gate, skip-if-missing via
      GODOT_BIN) wired into the export test (160/65 bones).
- [x] Optional: renamed `RIGIFY-` fcurve data-path prefix in
      `generate.py` to `RIGFORGE-` (2026-10-02); restore still heals
      legacy `RIGIFY-` paths, round-trip pinned in test_rename.py.
- [x] Review fixes (2026-10-03): cleanup Mirror now flips Rigify
      `.L.001` twist/face names (was leaving them on the wrong side;
      reuses utils.naming.mirror_name); mobile face anchor reads
      armature-space `head_local` (Bone.head is parent-relative);
      mobile weight merge is one pass over verts (was one per dropped
      group, ~95x). Verified offline (name table + randomized
      old-vs-new merge equivalence); Blender suites not re-run here.
- [ ] Optional: upstream watch — Blender keeps fixing Rigify; review
      new upstream commits periodically and hand-port (re-apply by
      judgment, never `git merge`: this tree has structurally diverged).
