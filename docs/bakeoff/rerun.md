# Bake-off re-run: fitter in the loop (2026-10-01)

Re-runs the rigforge column of [final.md](final.md) with the
deterministic landmark fitter in the loop (`tools/auto_rig.py`,
single command, strict gate, zero touch). unirig/Mixamo columns are
unchanged from the 2026-09-30 run — this re-run answers only whether
the fit-delta closed and what zero-touch rigforge costs now.

## Results

| | Andras hero | Stalker creature |
|---|---|---|
| command | `auto_rig.py andras_body_raw.glb --preset hll_hero` | `auto_rig.py stalker.glb --preset hll_stalker` |
| wall time | 11.6 s | 9.3 s |
| touch | 0 (one command) | 0 (one command) |
| fit-delta before | up to 6.1 cm (wrist/hand) | up to 54.1 cm (rear foot) |
| fit-delta after | 0.0 on all 10 joints | 0.0 on all 14 joints |
| gate | PASS, 23 drivers, min depth 12.4 mm | PASS, 27 drivers, min depth 17.6 mm |
| rig | 706 bones / 160 DEF | 454 bones / 80 DEF |
| bind | envelope transfer (direct heat: no solution), 494 gap-fills | envelope transfer, 0 gap-fills |
| rfcheck | clean (160 joints, 1 mesh, max 4 infl/vert) | clean (80 joints, 1 mesh, max 4 infl/vert) |
| Godot 4.7 import | 1 skeleton, 160 bones, 1 mesh | 1 skeleton, 80 bones, 1 mesh |

Artifacts (not committed): `/tmp/bakeoff_rerun/andras_rigged.glb`,
`/tmp/bakeoff_rerun/stalker_rigged.glb`, `hero.log`, `stalker.log`
(full fit/gate reports); `/tmp/bakeoff/godot/` holds the imported
copies (`andras_rigged.glb`, `stalker_rigged.glb`). `/tmp` does not
survive a reboot.

## Verdict update

- The zero-touch gap is closed on both reference subjects: the
  per-character fit-delta that manual work used to close (hands ~5 cm
  off on Andras, up to 54 cm on the stalker preset) now solves to 0.0
  inside the pipeline, verified by the strict gate.
- Wall time is ~12 s / ~9 s fully headless — under unirig's ~7 min on
  the hero, with 160 vs 28 joints (face, fingers, twists included).
- Caveats carry over from final.md: same inputs as the rigforge half
  (raw Andras A-pose, stalker blockout), one subject per class. No
  `BakeAnim` clip in these GLBs (auto_rig exports the bind pose; baked
  anims remain a `test_game_export.py` path, not a pipeline stage).
- Bind note: both subjects take the envelope-transfer rung (direct
  heat finds no solution on the stacked-shell hero join and the
  17-island creature join) — the documented ladder working as
  designed, not a regression.
