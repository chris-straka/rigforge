# Bake-off: rigforge half (interim, 2026-09-30)

Workflow per subject, fully headless: HLL preset metarig (used as
shipped, no per-subject tweaking) -> generate -> bone-heat bind ->
one-click game export -> rfcheck -> Godot 4.7.1 import.

## Andras (hero, 97k-vert mesh)

- Metarig 159 bones -> rig 706 bones -> **160 DEF joints**, 1 mesh.
- Timings: generate 2s, heat bind 20s, export 2s (~24s machine time).
- Weights: 132174 exported verts, 0 zero-weight, influences 1-4
  (54% at 4), worst weight-sum deviation 0.0000.
- rfcheck: clean. Godot: 1 skeleton, 160 bones, `BakeAnim` clip plays.
- GLB 42MB (mesh + textures).

## Stalker (creature, 17-part blockout)

- Metarig 70 bones -> rig 454 bones -> **80 DEF joints**, 17 meshes.
- Timings: generate <1s, heat bind 1s, export <1s.
- Each part bound separately (no asset edits): 1360 vertex groups
  total. Weights: 13030 verts, 0 zero-weight, mostly 1-2 influences
  (expected: rigid parts), sums exact.
- rfcheck: clean. Godot: 1 skeleton, 80 bones, `BakeAnim` clip plays.

## Tweak accounting (honest version)

Machine time is ~25s per subject and not the point. Human tweak time
in this run: zero — presets went in as authored. The *remaining*
manual delta a rigger would close by hand (see overlay renders):

- Andras hands land ~5cm inboard/high of the mesh hands.
- Stalker per-part bind can seam-split under big poses; an artist
  might join-then-bind instead (~minutes, one decision).
- Deletable nub bones (mane fingers, ears, breast copies): seconds.

Preset R&D (measuring subjects, building the generator) is amortized
tooling cost, not per-character time — same category as training a
model. The fair per-character comparison against unirig is: their
inference + cleanup vs our fit-delta + generate + bind.

## Still pending

- unirig half (granite-larissa running; stalker weights under
  diagnosis on their side).
- Mixamo hero baseline (owner uploading `mixamo_andras.fbx`).
- Final comparison table once all three land.
