# Bake-off final: rigforge vs unirig-mac vs Mixamo (Andras hero)

Date: 2026-09-30. Inputs differ slightly (see caveats). All three import
clean in Godot 4.7.1 headless.

|                       | rigforge        | unirig-mac      | Mixamo          |
|-----------------------|-----------------|-----------------|-----------------|
| joints                | 160 (DEF-)      | 28 (bone_*)     | 33 (mixamorig:) |
| fingers               | full            | none            | index chain only|
| face                  | yes             | no              | no              |
| twist bones           | yes             | no              | no              |
| wall time             | ~30 min scripted| ~7 min, 0 touch | ~15 min web UI  |
| skin                  | auto-weights    | 86.4% self-chk  | ML auto-rigger  |
| anims in GLB          | BakeAnim        | none            | T-pose clip     |
| Godot import          | ok              | ok              | ok              |
| rfcheck               | clean           | R_JOINT_PREFIX  | R_JOINT_PREFIX  |
| creature (stalker)    | 80 bones, clean | 12 jnts, weak   | N/A (biped only)|
| runs                  | local (Blender) | local (torch)   | Adobe cloud     |

## Verdict

- Hero quality: rigforge. Only one with face + fingers + twists.
- Automation: unirig. 7 minutes, zero touch, clean body rig.
- Convenience + mocap library: Mixamo. Weakest rig, but unlocks
  the animation library via retarget, which neither local tool gives.
- Recommendation (unchanged): hybrid. Rigforge heroes, unirig
  volume/background, Mixamo mocap looted via retarget onto HLL rigs.

## Caveats

- Mixamo ran on v3 (24k verts, T-posed, 1.5x); unirig on raw 97k
  A-pose; rigforge on raw via hll_hero preset. Not identical inputs.
- rfcheck prefix findings on unirig/Mixamo are contract mismatches
  (HLL DEF- convention), not quality verdicts.
- Mixamo license: free incl. commercial games via Adobe account;
  user holds the account, not the repo.

## Artifacts (not committed)

Binaries stay out of the repo. Originals live in `~/Downloads/bakeoff/`
(GLB/FBX/PNG) and `/tmp/bakeoff/` (unirig GLBs, Godot-checked GLBs);
`/tmp` does not survive a reboot. Interim rigforge notes:
[rigforge_half.md](rigforge_half.md).


- rigforge_andras.glb / rf_hero_fit_*.png — rigforge hero
- unirig GLBs in /tmp/bakeoff (peer originals), fit PNGs here
- mixamo_andras_v3_t_pose.fbx — Mixamo rigged download
- mixamo_andras.glb in /tmp/bakeoff/godot — Godot-checked GLB
- mixamo_fit_front.png — Mixamo result preview
- mixamo_andras_v4.fbx — unused fallback (v3 worked)
