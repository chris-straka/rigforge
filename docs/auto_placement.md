# Auto-placement research: closing the zero-touch gap

Date: 2026-10-01. Status: research + Phase 1 prototype
(`tools/detect_landmarks.py`) + Phase 2 fitter (`tools/fit_metarig.py`,
see Section 7) + Phase 3 validation gate (`tools/validate_fit.py`,
hardened against eyeball findings — see Section 8).

## 1. The gap (what "zero-touch" means here)

The bake-off ([bakeoff_final.md](/Users/c/Downloads/bakeoff/bakeoff_final.md))
split the verdict three ways:

| | rigforge | unirig-mac | Mixamo |
|---|---|---|---|
| hero joints | 160 DEF (+face/fingers/twists) | 28, body only | 33, weak |
| wall time | ~30 min scripted | ~7 min, 0 touch | ~15 min web UI |
| per-character manual delta | fit-delta (e.g. Andras hands ~5 cm off) + bind | cleanup only | retarget setup |

rigforge wins quality; unirig wins automation. The zero-touch gap is
exactly the per-character **metarig fit-delta**: today a preset goes in
as authored (`tools/make_hll_presets.py` constants measured per subject
*class*), and a rigger would still nudge bones per subject. Close that
delta and rigforge keeps its quality lead with unirig-class touch time.

Non-goal: replacing the template. Fixed-topology metarigs are what give
us face, fingers, twists, and IK/FK controls. Auto-placement fits the
template to the mesh; it does not invent topology.

## 2. Findings: how unirig places joints (read-only study)

Source: `~/SWE/unirig-mac` tree (another session owns it; nothing written
there) + its PORT.md / bake-off half.

- **Skeleton stage** (`src/model/unirig_ar.py`): a GPT-like causal
  transformer autoregressively emits a skeleton token sequence
  (**Skeleton Tree Tokenization**, `src/tokenizer/tokenizer_part.py`):
  joint positions discretized into bins over a continuous range, plus
  branch/bos/eos/pad, part-name, and class tokens. Constrained decoding
  (`VocabSwitchingLogitsProcessor`: only "next possible" tokens are
  unmasked) guarantees a topologically valid tree.
- **Conditioning**: the input mesh is sampled to a point cloud, normalized,
  and shape-encoded (Michelangelo / Pointcept PTv3 encoder). No template,
  no landmarks — placement knowledge lives entirely in the ~1.4 GB
  skeleton checkpoint (Articulation-XL2.0 training).
- **Skin stage** (`src/model/unirig_skin.py`): bone-point cross-attention
  predicts per-vertex weights from the predicted skeleton + geometry.
- **Observed behavior**: hero 28 joints in ~1.5 min + skin ~45 s on M4 CPU;
  stalker creature 12 joints, weak (bake-off). Output is stochastic
  (seed-dependent sampling), body-only (no fingers/face), and sparse.
- **License note**: both inference paths run `src/model/michelangelo/`,
  which is GPL-3.0 (own LICENSE). Self-hosted inference is fine, but only
  *joint coordinates* (data) should ever cross into this repo — never code.

Takeaway: ML placement buys category-agnostic, template-free joints at the
cost of sparsity, stochasticity, and a multi-GB dependency. rigforge wants
the *interface* (mesh in, joints out, zero touch) with the template's
richness — which is achievable deterministically because our topology is
fixed and our subjects are known classes (stylized biped, quadruped).

## 3. Findings: how rigforge fits today

- Presets are **generated modules** (`rigforge/metarigs/hll_hero.py`,
  `hll_stalker.py`, 159 / 70 bones): stock metarig + rigid,
  square-preserving subtree transforms from class-level constants
  (`HERO_HEIGHT`, `HERO_ARM_DROP_DEG`, stance widths), encoded with
  `rigforge.utils.rig.write_metarig`.
- Fitting machinery already exists and is reusable: `descendants()` /
  `apply()` / `about()` (subtree rigid transforms about pivots) and
  `reconnect_from()` (restore `use_connect` where joints stayed closed).
- Available bone helpers: `rigforge/utils/bones.py` (`put_bone`,
  `copy_bone_position`, `align_bone_*`, chain axis tools).
- Validation precedent: `tests/test_presets.py` asserts facing axis,
  limb spread, height bands — the same assertions a fitter can check
  before/after.

So the missing piece is narrow: **per-subject landmark measurement** to
drive the existing transform machinery, instead of per-class constants.

## 4. Proposal: mesh-analysis-based metarig fitting

Deterministic, headless-safe, dependency-free (stdlib + `bmesh` + Blender's
bundled numpy). No ML, no checkpoints, runs wherever Blender runs.

### 4.1 Pipeline

```
mesh (joined, transforms applied)
  -> 0. preconditions: facing axis, up=+Z, single component check
  -> 1. coarse fit: bbox scale preset to subject height (as today)
  -> 2. landmark detection: slice/blob analysis (Phase 1, DONE prototype)
  -> 3. correspondence: landmarks -> driver-bone head/tail targets
  -> 4. solve: rigid subtree transforms, joints closed, symmetry enforced
  -> 5. validate: inside-mesh / symmetry / closure checks + fit report
  -> fitted metarig -> generate -> bind (unchanged downstream)
```

### 4.2 Landmark detection (slice/blob analysis)

For a biped in A/T-pose, horizontal slices top-to-bottom show a blob
signature: `1 (head) -> 3 (torso+arms) -> 1 (torso) -> 2 (legs) -> 2 (feet)`.
Blob-count transitions locate `neck_z`, `shoulder_z` (armpit merge),
`crotch_z`; blob centroids locate limb axes; cross-section minima along a
limb axis locate elbow/knee/wrist/ankle. For a quadruped the same idea runs
longitudinally (spine axis) plus leg detection from below.

Concrete landmark set:

- hero: `head_top`, `neck`, `shoulder_{L,R}`, `elbow_{L,R}`, `wrist_{L,R}`,
  `hand_tip_{L,R}`, `crotch`, `knee_{L,R}`, `ankle_{L,R}`, `toe_{L,R}`, height.
- stalker: `spine_front`, `spine_rear`, `neck_base`, `head`, `skull`,
  per-leg `top/mid/foot/toe`, `tail_base`, leg stance `(x, y)`.

Algorithm notes (all demonstrated in the prototype):

- Slices: ~120 z-bins; per-slice blobs via union-find over mesh edges
  whose endpoints both fall in the band. Records count/centroid/radius.
- Transitions: scan blob-count changes; refine z by bisection on count.
- Limb axes: PCA (numpy) on verts assigned to a tracked limb blob.
- Joint z along a limb: minima of the cross-section-radius profile.
- Robustness: median-filter profiles; require symmetric L/R blob pairs
  before trusting arm/leg landmarks; fail loud (named landmark + slice
  evidence) instead of guessing.

### 4.3 Correspondence + solve (Phase 2)

Driver bones take landmark targets; everything else follows rigidly:

- hero drivers: `spine..spine.006` chain, `shoulder.*`, `upper_arm`,
  `forearm`, `hand` (+fingers/fan rigid follow), `thigh`, `shin`, `foot`,
  `toe`. Face bones: rigid follow of `head` (face placement from mesh
  features is explicitly out of scope — presets already fit).
- stalker drivers: `spine.001..006`, `neck.001..004` + `head`/`skull`,
  per-leg chains, `tail.001..005`. Head/neck stay a connected rigid chain
  (the head rig requires it — same constraint as in `make_hll_presets.py`).

Solve order per chain (root outward): set head from parent/proximal
landmark, set tail from distal landmark, then rigid-move the subtree for
the residual (reusing `about(pivot, linear)`); restore `use_connect` where
joints closed (reusing `reconnect_from` logic); mirror `.L` from `.R`
(or average) to enforce symmetry; preserve rolls (recompute only if a
bone's direction changed > ~5 deg, via `align_bone_z_axis`).

### 4.4 Validation (Phase 3)

A fitted metarig must pass before it is allowed near `generate`:

- **closure**: every driver joint gap < 1e-4 (same epsilon as
  `reconnect_from`) or explicitly listed as intentionally open.
- **inside-mesh**: each driver bone midpoint raycasts inside the mesh
  (parity check, `object.ray_cast`); report bone names + distances on fail.
- **symmetry**: L/R landmark mirror error < tolerance (default 1 cm).
- **fit report**: per-driver-bone surface distance stats + overlay render
  paths, so the bake-off "fit-delta" becomes a number that must shrink.

Fallback ladder (never silently ship a bad fit): full auto -> landmark
report for artist confirm -> preset as-is + report. A failed validation
stops the pipeline with bone names and what to fix (this also serves the
TODO "error diagnostics" item).

### 4.5 Hybrid option (unirig joints as landmarks)

Same fitter, different landmark source: run unirig-mac skeleton inference
on the subject and feed its ~28 predicted joints in as *landmark hints*
for the corresponding driver bones (needs a unirig-name -> metarig-bone
correspondence map, ~20 entries for a biped). This is the strategy hub's
endgame ("auto-placement feeding hand-grade output") with a clean license
boundary (coordinates only). Recommended only if slice/blob landmarks
prove insufficient on real HLL meshes — try deterministic first.

## 5. Work plan

- [x] Phase 1: landmark detector prototype (`tools/detect_landmarks.py`):
      slice/blob analysis, JSON landmarks, headless, synthetic-mesh test.
- [x] Phase 1b (hero): detector run on the Andras hero mesh
      (`rigforge_andras.glb`, read-only); see Section 6. Stalker
      blockout still pending (needs the quadruped signature variant).
- [x] Phase 2: fitter (`tools/fit_metarig.py`): correspondence + solve,
      reusing `make_hll_presets` machinery; fitted preset must generate.
      DONE (Section 7): Andras fit-delta closed to 0.0 cm on all driver
      joints, fitted metarig generates headless (706 rig / 160 DEF bones).
- [x] Phase 3: validation gate + `tests/test_auto_placement.py` (fit,
      generate, export, fit-delta numbers); hardened per Section 8.
      Re-run bake-off comparison still open.
- [ ] Optional: `wm.rigforge_auto_place` operator + panel button (needs
      the same code headless-first; UI is a thin wrapper).
- [ ] Optional: hybrid landmark source (Section 4.5) if deterministic
      landmarks stall on some subject class.

## 6. Prototype status (this change)

`tools/detect_landmarks.py` implements Section 4.2 end to end: builds or
imports a mesh headless, runs slice/blob analysis, prints JSON landmarks.
Verified: synthetic A-pose biped `--check` passes (all 11 landmarks within
6 cm of the build spec), and the headless smoke test (`RIGFORGE_SMOKE_OK`)
plus `tests/test_presets.py` stay green — the addon itself is untouched, so
no regressions are possible outside the new `tools/` file. `ruff check`
and `ruff format --check` are clean on the new file.

Andras hero run (read-only input, envelope remesh at height/150):

```json
{"height": 1.1375, "neck_z": 0.9244, "armpit_z": 0.7253,
 "elbow_z": 0.6874, "wrist_z": 0.5547, "hand_tip_z": 0.4219,
 "wrist_x": 0.2263, "crotch_z": 0.4125, "knee_z": 0.2892,
 "ankle_z": 0.1186, "ankle_x": 0.0952, "facing": "-Y"}
```

Cross-checks against independent measurements: height matches
`HERO_HEIGHT = 1.14`; wrist (0.226, 0.555) vs the preset author's "mesh
hands ~ (+-0.26, z 0.45)" comment (wrist sits above hand center, as it
should); knee 0.289 vs preset thigh-tail 0.309; facing detected (not
assumed) as -Y from foot asymmetry. Four findings for Phase 2:

1. Production meshes need the envelope remesh: stacked shells (body +
   clothing + hair cards) read as 20-120 blobs per slice; the fused
   exterior restores the textbook `1 -> 3 -> 1 -> 2` signature.
2. Relaxed-A-pose arms merge with the torso in the envelope, so the arm
   run starts at the armpit (0.725), well below the shoulder joint: Phase 2
   must back-project the shoulder along the tracked arm axis instead of
   reading it from the separation point.
3. Boot/shaft clothing hides the ankle dip (0.119 lands mid-boot, not at
   the ~0.05 joint): Phase 2 should place the ankle from sole offset when
   `ankle_dip` is weak. Same caution for gauntlet wrists (`wrist_dip`).
4. Finger splits below the palm must be excluded from arm tracking (done:
   wrist/elbow track count==3 slices only); `hand_tip_z` still reads the
   fingertip floor correctly.

## 7. Phase 2 status (this change)

`tools/fit_metarig.py` implements Section 4.3 end to end: subject mesh +
landmark JSON in, fitted `hll_hero` metarig module out (headless), with
an in-session headless generate as proof. Reuses `descendants()` /
`apply()` / `about()` / `reconnect_from()` from
`tools/make_hll_presets.py` and the analysis (`import_mesh` /
`envelope_copy` / slice tracking) from `tools/detect_landmarks.py` —
no reinvented machinery. Solve is root-outward per driver chain onto
landmark targets, `.L` solved and `.R` mirrored, rolls preserved unless
a bone's direction changed > 5 deg (then the z-axis realigns to the
pre-fit world orientation).

Section 6 findings, resolved:

1. Envelope remesh input: analysis reuses the detector's joined-mesh +
   envelope path, so fitting runs in landmark coordinates directly.
2. Shoulder back-projection: shoulder = elbow + arm-axis-up x preset
   upper-arm length — lands at z 0.864, 14 cm above the armpit run
   start (0.725), as predicted. Upper arm holds elbow y (tracked flat);
   a guard clamps the shoulder above armpit + 2 cm.
3. Ankle sole offset: fired on Andras via the implausible-z trigger
   (0.1186 > 0.085 x height; dip 0.0358 was strong but mid-boot),
   ankle placed at zmin + 0.044 x height = 0.0502. A weak-dip trigger
   (< 0.010) covers boot-smoothed profiles; both triggers verified
   with a doctored JSON. Gauntlet wrists get the same pattern
   (hand_tip + palm length when wrist_dip < 0.004); Andras wrist_dip
   0.006 reads weak-but-usable, so the measured 0.5547 stands —
   the arm profile shows a textbook wrist (taper -> waist at 0.545 ->
   palm flare -> finger splits at 0.479).
4. Finger exclusion: arm axis fits the 18 clean count==3 bands above
   the wrist only; leg axis fits above the foot flare (z > 0.11 x
   height, 31 bands).

Andras fit-delta (driver-joint distance to target, cm), before = preset
as-is, after = fitted:

| joint | before | after | joint | before | after |
|---|---|---|---|---|---|
| shoulder | 1.9 | 0.0 | hip | 0.2 | 0.0 |
| elbow | 3.4 | 0.0 | knee | 4.1 | 0.0 |
| wrist | 6.1 | 0.0 | ankle | 4.3 | 0.0 |
| hand | 6.1 | 0.0 | neck base | 2.8 | 0.0 |
| | | | head top | 0.0 | 0.0 |

The "hands ~5 cm off" delta was real (preset forearm 17.5% too long —
`HERO_ARM_STRETCH` overshoot); thigh +6.4%, shin -8.1%. Neck base moves
down 2.8 cm; the head rig requires spine.004 coincident with the chest
top, so the neck delta is distributed across the torso (k = 0.924) and
the neck chain stretched to the head top (k = 1.154) instead of
detaching the neck — generate fails loudly otherwise (observed).
Closure max gap 0.0, symmetry 0.0, input L/R asym 4.4 mm (< 1 cm);
rolls realigned on 4 limb bones per the 5-deg rule.

Validation beyond Andras: synthetic-mesh run (12.4 cm wrist delta,
no fallbacks, no cautions) fits and generates 706/160 — the machinery
is not overfit to one subject. Torso has no direct landmarks (follows
coarse scale + neck distribution); limb y uses measured centroids only
where sections are slim and interpolated, else band/preset values;
foot/toe are rigid (no toe landmark yet); face rigid-follows the head.
Full gate (inside-mesh raycasts, overlay renders) stays Phase 3 work.

Verified: headless smoke (`RIGFORGE_SMOKE_OK`) and
`tests/test_presets.py` green; `ruff check` + `ruff format --check`
clean on the new file. Fitted module (159 bones, write_metarig format)
and session blend are build artifacts under `/tmp`, not committed.

## 8. Phase 3 gate hardening: four eyeball findings (this change)

Fitted-Andras overlay eyeballing (`gate_*.png`) caught four placements
the gate missed or mis-reported. All fixes live in
`tools/validate_fit.py` + `tests/test_auto_placement.py`; the gate
stays warn-only for placement (hard fails are still closure /
inside+margin / symmetry only).

1. FINGERS (fan offset laterally, thumb out): the gate measured chain
   angle only. Added fingertip containment (any of the four tips
   outside the envelope warns) with per-finger lateral-vs-axis vectors
   naming the nudge. Andras now warns `angle + outside:all four`
   (pinky 46.2 deg, laterals 3.2-4.3 cm, all tips 0-vote outside)
   vs synthetic ok (tips inside, pinky 38.4 deg = the preset's natural
   fan). Two calibration notes: (a) the old sub-wrist vert set caught
   266 boot-sole verts at z~0, corrupting the reference — finger
   analysis now uses a hand slab (wrist down to fingertip floor);
   the old "pinky 32 deg fine" reading is obsolete, superseded by the
   decontaminated 46 deg on that same bone. (b) No lateral threshold
   exists: healthy synthetic reads 4.9 cm (fat hand box, same medial
   fitter bias as Andras's 4.3 cm), so laterals stay diagnostic.
   Thumb: included in the angle check (Andras 25.5 deg, synthetic
   22.6 deg — it tracks the fan; the old "points sideways" rationale
   was wrong for this metarig). Thumb tip grazes 6 mm off the surface
   (knuckle exits at 0.2 mm depth): real but mm-scale, reported
   ungated; the cm-scale fan offset is the finding.
2. FACE (bones riding into forehead/hair): the gate checked head-top
   only. Added a warn-only face heuristic: face-bone centroid must sit
   between a chin floor (lowest single-blob band above the neck) and
   the skullcap, face bottom above the chin floor; brow clearance,
   face frac, and per-part ring containment reported, never gated.
   Honest limit: calibration forbids a riding-high threshold —
   healthy synthetic reads -0.3 cm brow clearance vs Andras's +2.1 cm
   (hair inflates the skullcap reference the wrong way), so the gate
   trips only on gross misplacement. Andras passes the tripwire while
   the eyeball finding stands (eyes 1.6 cm above the widest ring, brow
   in the narrowing hair-spike bands): the real fix is fitter-side, a
   hair-robust head-top target (skullcap instead of zmax). Slices
   cannot see mesh features (eyes/nose/mouth) and cannot separate hair
   from skull — that stays out of scope.
3. TOES (wrong direction + short of foot end): the gate checked
   tip-inside + overshoot only. Added toe-axis yaw vs mesh foot
   direction (front/back-half split; warns past 45 deg, same pattern
   as fingers). Andras yaw reads 2.7 deg — direction is fine; the
   sideways stubs the eyeball read as toes are the heel.02
   reverse-foot pivots, correct by design. The genuine toe issues are
   shortfall (tip 3.3 cm short of the foot front, reported, ungated —
   healthy synthetic reads 6.7 cm on its crude foot box) and the hard
   margin fail (7.2/8.5 mm < 1 cm, eyeballed a real breach).
4. MYSTERY STICK (red tube between the legs): the labeled close-up
   (`--closeup PNG` + `_labels.json` pixel coords; text overlaid with
   e.g. `magick render.png -font /System/Library/Fonts/Supplemental/Arial.ttf
   -pointsize 20 -fill white -undercolor '#000000A0' -annotate
   '0x0+X+Y' 'bone' ... labeled.png`) names it `Bone`: the
   `armature_add` default bone (0,0,0)-(0,0,1), which `create()` never
   removes. Verdict: real stray bone, not a render ghost — but an
   artifact of the gate's rebuild helper, invisible to every check
   (parentless, non-driver, unmirrored) and absent from the fitted
   metarig itself. Fixed by clearing edit bones before `create()` in
   `build_metarig_from_module`; regression test asserts the rebuilt
   set equals the fitted set (159 bones, no `Bone`).

Threshold justifications, tied to verdicts: `FINGER_WARN_DEG` 45 kept
(above the 38 deg natural fan; corrected Andras pinky 46.2 deg fires
correctly); `MARGIN_DEFAULT` 1 cm kept (7.2 mm toe breach deemed
real); `HEAD_WARN` 3 cm kept (Andras -1.3 mm; hair-inclusive by
fitter contract); `TOE_WARN` 2 mm kept (no overshoot observed);
`TOE_DIR_WARN` 45 new (same-pattern yaw; Andras 2.7 deg, synthetic
0.0 deg, twisted-toe red test 90.0 deg). No lateral / shortfall /
brow thresholds — each was attempted and killed by calibration (see
above), not by preference.

Fitter follow-ups out of gate scope: rigid preset toe (length never
fit — needs a toe landmark), hair-robust head-top target, finger-fan
orientation (knuckles on the mesh column, tips diving medially —
rigid follow keeps the wrong fan angle).

Verified: `tests/test_auto_placement.py` green incl. per-check
red-on-known-bad coverage (shifted fan, floated face, twisted toe —
and the old angle check proven blind to pure translation on the same
bad fit); smoke (`RIGFORGE_SMOKE_OK`) and `tests/test_presets.py`
green; `ruff check` + `ruff format --check` clean on both touched
files. Andras final: hard FAIL on toe margin (unchanged), fingers
warn, toes/face warn-quiet with diagnostics, head ok.

## 9. Fitter follow-ups: production meshes through the strict gate

The Section 8 gate failed every production mesh (Andras: toe margin;
Hunyuan elf: shoulder/foot/toe outside; Hunyuan millennial: untested
merge case). All fixes below are fitter-side
(`tools/fit_metarig.py`); the gate's thresholds and verdicts are
untouched — where a threshold looked suspect it was held and the
fitter was fixed instead (see "thresholds" below).

Principle: verify-then-correct. Prospective driver midpoints raycast
against the envelope with the gate's own `point_depth` (margin read
from `validate_fit.MARGIN_DEFAULT`, never duplicated), on both sides
(the `.L` solve mirrors to `.R`, so `.R` midpoints verify too).
Corrections apply only to failing bones, so passing fits solve
exactly as before. Every correction is bounded, logged in
`fallbacks`/`cautions`, and re-verified; failures stay honest.

Fixes:

- Robust arm bands. Outer-blob validity (`n >= 12`, `r >= 0.012 m`)
  replaces the count==3 filter: speck bands (ponytail splits, hair)
  drop out while true upper-arm bands in count>3 slices join. Legacy
  filters stay as fallback rungs (`band_mode` reports which won).
- Elbow re-derive. The detector's elbow is re-found on the robust
  profile and adopted past 5 mm of disagreement (its band was
  speck-dragged: smoothing dips at the first clean band below noise).
- Shoulder v2. Back-projection runs along the upper-arm ray (bands
  above the elbow, full 3D — the old flat-y forearm projection
  overshot medially on hanging arms and rode the front surface on
  leaning arms). Legacy flat-y stays a candidate; the deepest passing
  placement wins, else a shortening march (100% -> 40% of preset
  length, armpit clamp kept).
- Ankle v2. The sole fallback gains a forefoot trigger (dip above
  dorsum + 0.025 m is a shin narrowing, not a joint) alongside the
  weak-dip and 0.085-height triggers; sole ratio 0.044 -> 0.048
  (anatomical ~8 cm at 1.7 m plus gate headroom); a prospective
  foot/toe chain landing outside forces the fallback regardless.
- Hand mesh-fit. When the preset hand vector lands outside (stubby
  merge-shortened hands), the tail re-aims along the measured finger
  column with a span-fit length instead of dangling past the fingers.
- Verify-nudge. Failing foot/hand tail targets step 2 mm off the
  nearest raycast surface (max 8 steps, 2 mm past the margin for
  headroom), never un-passing a passing midpoint.
- Finger re-aim. Each chain rotates about its knuckle onto its own
  mesh column (thigh-safe asymmetric slab, thumb-masked,
  lowest-flesh anchors for splay), scales to its flesh end, shifts
  onto the column, and searches for flesh; per-finger revert on
  failed verify (tip inside 1 mm+ both sides, gate angle < 45 both
  sides). Untouched when already quiet.

Before/after (strict `auto_rig.py`, no `--no-validate`):

| subject | before | after |
|---|---|---|
| Andras | FAIL: toe.L/R margin 7.2/8.5 mm; pinky 46.2 deg + tips outside | PASS (min 12.4 mm); fingers quiet |
| elf | FAIL: shoulder/foot/toe L/R outside | PASS (min 10.5 mm); fingers quiet; elbow re-derived, shoulder marched 0.75, ankle forefoot trigger |
| millennial | FAIL (untested): shoulder/hand outside, upper-arm margin 3.6 mm, toe.R 9.0 mm | PASS (min 12.6 mm); hand mesh-fit + nudge; finger angle near-miss 45.5/46.9 (open, below) |

Thresholds: none believed wrong. `MARGIN_DEFAULT` 1 cm fired on real
misplacements in every case (medial toe hug, air-gap shoulder, sole
graze); `FINGER_WARN_DEG` 45 fired on the 46-68 deg legacy fans and
cleared on the 2-9 deg fitted ones; `HEAD_WARN` 3 cm correctly flags
the elf ear-spike (open fitter item, not a threshold issue). No
render showed a correctly placed bone failing a check.

Open follow-ups: hair-robust head-top (elf ears stretch the head
3.8 cm, warn-only); mill finger angle near-miss (gate slab catches
the thigh, contaminating its reference a few degrees — fitter-side
polish toward a contaminated reference was declined); mill finger
deform caveat (5 cm stub hand vs 13 cm preset hand+fingers: gated
hand bone fits, finger chains warn honestly); rigid toe length
(shortfall still reported, ungated).

Verified: `tests/test_auto_placement.py` §9 green on all three
subjects (skip-if-absent) plus the synthetic contract unchanged;
`auto_rig.py` strict exits 0 with GLBs out on all three, each
`rfcheck`-clean (160 joints); smoke, presets, game-export, rename
suites green; ruff clean on both touched files.
