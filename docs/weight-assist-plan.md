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

Use the deformation test (`~/SWE/retopoforge/docs/deformation-test.md`),
as implemented in `tests/test_weights_w1.py` (metrics: p95 stretch,
area distortion (mean |log area ratio|, rigid-invariant candy-wrap
proxy — closed-slice volumes are ill-defined under weight-threshold
boundaries), flips; poses in `tests/fixtures/weights_poses/`):

- Tube (clean single joint): voxel beats Blender automatic weights on
  p95, area distortion, and flip count at bends <= 90 deg. Shipped
  green (elbow_90: p95 1.12 vs 1.19, ad 4.2% vs 7.8%, flips 47 vs 76).
- Robustness (no heat baseline — heat exceeds usable budgets on
  degenerate soup): voxel binds production soup, mm-scale meshes, and
  far-off rigs with bounded fallbacks, bit-identical re-binds, and
  smoke-grade deformation (p95 < 2.5, ad < 25%, flips < 15%).
- Bends past 90 deg are report-only: flip-free LBS past 90 deg needs
  weight gradient under cot(bend/2)/2r (at 135 deg a 24 cm blend on
  the 50 cm tube), so both binders crush and compare as noise.
- Hero/quad are tracked report-only pending W1b (below).

Nudge strokes must update in under 1 s on a 15k-triangle mesh; all
outputs deterministic.

## W1 findings (2026-10-01)

- Blender 5.2 heat converges on most adverse inputs (doubled shells,
  slits, open meshes, needles, mm scale, bones 20 cm off) but goes
  pathological on degenerate soup: ~15 min on zero-area soup, 20+
  min (killed) on combined needles+slits+doubled+chunk. Voxel binds
  the same soup in ~1 s, every time. Open meshes are the honest
  limit: parity leaks, so the operator cancels with "not closed"
  (the envelope flag fuses stacked closed shells, it cannot put
  volume into an open shell).
- Hero/quad parity needs kernel R&D, not tuning (all cheap fixes
  falsified: finer cells, wider/narrower smoothing, smoothed
  fixtures, tighter cutoffs). Diagnosis: the span-normalized kernel
  has no distance decay (0.22 weight leaks to distant bones), the
  physical blend width is global (tube wants narrow, hero wants
  wide/narrow per joint), and top-4 membership churns across 160
  bones (1.8x medium weight jumps vs heat). Path: soft per-bone
  geodesic decay plus adaptive blend width.
- Scale bugs found by the robustness suite and fixed: smoothing
  passes are capped relative to mesh extent (mm meshes computed a
  million passes), bones outside flesh are excluded (rigid
  Euclidean-nearest follow instead of 50/50 mush).

## W1b findings (2026-10-02): tuning frontier mapped, ceiling found

16-run sweep over decay strength, kernel power/sharpness, smoothing
width, output k, adaptive k/power, field/output-k split, and
post-sparsify re-smoothing. Falsified: softmax kernel, wide field-k,
adaptive k (per-vert cliffs explode p95), adaptive power, re-smooth.
Promoted (all green: tube strict, robustness, operator, smoke):
power 6 (crisp commitment), decay len/4 (far-bone fade), 3x blend
width. Hero bends improve strongly (elbow p95 now beats heat;
knee/shoulder flips beat heat) but full parity is out of reach for
any global setting: shoulder p95 and neck twist need soft multi-way
blends while bends need crisp commitment. Remaining gaps need a
solver upgrade (bounded-biharmonic-class), not more tuning — queued
as optional W1c. Heat itself wobbles run to run (p95 ±1e-4, flips
±1), so hairline wins stay ungated.

## Phases

| Phase | Builds | Gate |
|---|---|---|
| W1 | Geodesic voxel auto-weights (tube + robustness) | shipped 2026-10-01, gate green |
| W1b | Kernel tuning: best-safe defaults | shipped 2026-10-02, bends near-parity, rest tracked |
| W2 | Nudge strokes | shipped 2026-10-02, solver + operator green (below) |
| W2 findings (2026-10-02) | Logit-space Jacobi + softmax refill; value pins are absolute (complement pinned: proportional, else nearest-present summon); excludes get a two-pass whole-row feather (pass 1 finds band redistribution, pass 2 ramps band→seed over 5 rings); exact pins rejected (crease), exact excludes rejected (reassignment cliff); pins stored as JSON on the mesh; undo static-gated (no undo ctx headless). Repair gate: damaged elbow p95 4.5→1.2, flips 222→59; re-solve 0.08 s at 15k tris. |
| W3 | Piece transfer with inpainting | shipped 2026-10-02, solver + operator green (below) |
| W3 findings (2026-10-02) | Nearest-triangle barycentric copy within 5%-of-body radius, W2 solver inpaints the rest (copied verts as pins; transfer solves unseeded so stale piece weights cannot fight the pins); unseeded verts warm-start at the pins' mean row (floor init needs 100s of passes over long hangs); all-far pieces seed the nearest vert; operator copies the body's armature modifier. Cape gate: naive hem pulls to Bone (fixture discriminates), inpainted hem rides Bone.001 rigidly (bend err 0.0), jump 0.0. |
| W4 | Cleanup ops | shipped 2026-10-02, rows + operator green (below) |
| W4 findings (2026-10-02) | Pure row transforms (limit top-k w/ name-asc ties, normalize, prune w/ keep-max fallback, fixed-pass weight-space smooth, KDTree X-mirror w/ .L/.R+_L/_R swap) + one modal operator; dict-rows need rows_to_assignment before apply_nudge (dict iteration unpacks "B2" into ("B","2")); Blender clamps paint to [0,1] (no out-of-range fixtures); no exact-zero-x verts exist (cos pi/2 ~ 6e-17, center mirrors in place). 6-bone paint limits to exactly 4, normalized. |
