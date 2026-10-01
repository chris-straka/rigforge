# Weight assist — build plan

Written 2026-10-01. Goal: skin weights for one-off monsters and pieces
(capes, hair, armor) that come out usable automatically and that the
owner can fix by **nudging** instead of painting. Humanoids get weights
from the wrapped base (`~/SWE/wrapforge`), so this is for everything
else. Context: `~/Games/hll/tools/roadmap.md` section 3.3.

## Features, in build order

1. **Robust auto-weights**: Blender's heat weighting fails on messy,
   non-manifold, or multi-shell meshes. Implement geodesic voxel binding
   (Dionne & de Lasa 2013): voxelize the mesh, compute geodesic
   distances from each bone through the voxel volume, turn them into
   smooth, normalized weights. Works on any mesh soup.
2. **Nudge strokes**: the owner paints a few strokes with a bone picked
   ("this area follows the upper arm" or "not this bone"). Strokes
   become constraints; the solver re-fills smooth weights everywhere
   else (harmonic / bounded-biharmonic interpolation with the strokes
   as fixed values), live in the viewport, undoable.
3. **Piece transfer**: weights for capes/hair/armor from the body with
   robust inpainting (Abdrashitov et al., SIGGRAPH Asia 2023): copy
   where the piece is close to the body, inpaint smoothly where it hangs
   away (cape hem, long hair), instead of plain nearest-surface copy.
4. **Cleanup ops**: limit to 4 influences per vertex (Godot/mobile),
   normalize, mirror L/R, smooth, prune tiny weights.

## Architecture

Inside rigforge (Blender extension, GPL). Heavy solves (voxel geodesics,
sparse harmonic solve) may go to a small Rust or NumPy core if pure
Python is too slow; Blender ships NumPy.

## Gates

Use the deformation test (`~/SWE/retopoforge/docs/deformation-test.md`):
on procedural tube, biped and quadruped fixtures, the new auto-weights
must beat Blender's automatic weights on p95 stretch and volume loss at
every joint, with 0 flips; nudge strokes must update in under 1 s on a
15k-triangle mesh; all outputs deterministic.

## Phases

| Phase | Builds | Gate |
|---|---|---|
| W1 | Geodesic voxel auto-weights | beats Blender auto weights on the deformation test |
| W2 | Nudge strokes | owner fixes a bad elbow in < 2 min |
| W3 | Piece transfer with inpainting | cape hem moves smoothly, no body pull-through |
| W4 | Cleanup ops | 4-influence limit passes rfcheck mobile |
