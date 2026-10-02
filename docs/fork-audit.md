# Fork-or-add-on audit (2026-10-02)

Owner request (TODO, 2026-10-01): is rigforge a real fork of Rigify, or
new code sitting on top? If the latter, propose a companion add-on
that uses Blender's stock Rigify so Blender updates arrive for free.
**Report only — no restructure performed.**

## Verdict

**Companion add-on is feasible: rigforge only adds on top.** No
functional edit exists inside Rigify's own files. The entire fork
delta vs stock Rigify 0.6.10 is:

1. A mechanical module rename (`rigify` → `rigforge`, incl.
   coexistence operator/panel/menu id renames),
2. automated lint application (ruff format + safe autofixes),
3. 19 brand-new files (all features),
4. 5 new panels + 6 submodule lines inside 2 old files
   (`ui.py`, `operators/__init__.py`) — purely additive, trivially
   movable.

The one migration cost is **data, not code**: id-property names
stored in .blend files were renamed (`rigify_*` → `rigforge_*`),
so old .blends need a one-time prop migration to work under
stock+companion (details below).

## Method

Git archaeology, not fuzzy diffing: the tree contains the pristine
import (`1c6d709`) and both rename commits, so every line was traced
to its commit and classified.

- Baseline: `diff -rq` of `1c6d709:rigify` vs
  `Blender.app/.../addons_core/rigify` → identical except
  `__pycache__`. Day-0 import is byte-identical stock 0.6.10.
- `b5a0914` (bulk rename): full-tree diff with rename normalization
  (case variants + coexistence infixes), isort + format normalized →
  **0 lines remainder. Pure rename, proven.**
- `e967a39` (rename finish + ruff): same treatment; every remainder
  line classified by hand. Remainder = coexistence id renames
  (`object.armature_X` → `object.rigforge_X`,
  `ARMATURE_MT_%s` → `ARMATURE_MT_rigforge_%s`,
  `pose.convert_rotation` → `pose.rigforge_convert_rotation`,
  `VIEW3D_PT_rig_*` → `VIEW3D_PT_rigforge_*`), ruff format wraps,
  and semantics-preserving autofixes only: isort, UP006/UP007/UP008/
  UP026/UP032/UP037 (`Optional`→`| None`, `List`→`list`,
  `(object)` removal, `.format`→f-string, quote removal),
  F541 (placeholder-less f-string → string), PERF401
  (`if k in d: del` → `.pop(k, None)`), RUF100 (stale noqa).
  **No functional edit. Proven.**
- Feature commits (`7e08af8` … `34037ab`, 9 commits): `--name-status`
  shows the only old files ever touched are `ui.py` and
  `operators/__init__.py`; all hunks additive (new panel classes +
  `classes` entries; new submodule-list strings). Zero hunks in
  `rigs/`, `utils/`, `generate.py`, `base_*`, `metarig_menu.py`.
- Behavioral proof: generated the stock human metarig under **both**
  stock Rigify and the fork headless and diffed the rigs
  (`/tmp/rf_audit_gen.py`, outputs in `/tmp/audit/gen_*.json`):
  **bit-identical** — 706 bones (names, heads, tails, parents,
  deform flags, constraints, custom props), 198 drivers (paths,
  expressions, variables), 220 widgets, collections.
- `RIGIFY-` driver data-path prefix deliberately kept
  (`generate.py:330,456`), so drivers match stock exactly.

## New files (all additive, 19 total)

- `auto_place/` (6): metarig fitter + validators + unirig hints.
- `operators/`: `game_export`, `auto_place`, `voxel_weights`,
  `nudge`, `transfer`, `cleanup` (6).
- `weights/`: `__init__`, `voxel`, `nudge`, `transfer`, `cleanup` (5).
- `metarigs/`: `hll_hero`, `hll_stalker` (2, dir-scan discovered —
  no registration edit needed).

## Old-file edits (functional, all movable)

| File | Edit | Move-out |
|---|---|---|
| `ui.py` | 5 panel classes (game_export, voxel_weights, nudge, transfer, cleanup) + 5 `classes`-tuple entries | Move classes verbatim into the companion; stock `ui.py` untouched |
| `operators/__init__.py` | 6 submodule-list strings | Companion keeps its own submodule list; stock file untouched |

## Coupling analysis (for the companion design)

- **Old → new imports: none.** Zero `import`/`from` of new modules
  in any old file. Coupling is strictly one-directional.
- **New → old imports: one function.** Only
  `align_bone_z_axis` from `utils.bones` (used by
  `auto_place/fit_metarig.py`, `operators/auto_place.py`).
  Companion imports it from `rigify.utils.bones` — identical API.
- **Presets:** `metarig_menu.py` discovers metarigs by directory
  scan (`os.listdir`), so the HLL presets are pure file adds. A
  companion needs its own small menu hook to expose its own
  `metarigs/` dir (new code, no stock edits).
- **Generated rigs don't import the addon** (no `import
  rigify`/`rigforge` in generated output) and are bit-identical to
  stock output, so rigs blend freely in both directions.
- **Nudge pins** (`rigforge_nudge_pins` mesh id-prop) are
  fork-native data; the companion keeps the name, no migration.

## The one migration cost: renamed id-props in .blends

The rename covered **data-level** custom property names saved in
.blend files (armature, bone, collection id-props). Stock looks for
`rigify_*`; fork files carry `rigforge_*`:

- Per metarig bone: `rigforge_type`, `rigforge_parameters`
  (the critical pair — stock ignores untyped bones).
- Per armature: `rigforge_target_rig`, `rigforge_rig_basename`,
  `rigforge_rig_ui`, `rigforge_widgets_collection`,
  `rigforge_force_widget_update`, `rigforge_mirror_widgets`,
  `rigforge_colors`, `rigforge_layers`, `rigforge_generator`,
  `rigforge_wrapper`, `rigforge_action_slots`, `rigforge_ui_row`,
  `rigforge_active_feature_set`, `rigforge_parameters`, …
- Session-only (not saved, no issue): all `rigforge_*`
  WindowManager props, panel/operator bl_idnames.

Impact: existing fork .blends opened under stock+companion would
lose their rig-type assignments (stock sees untyped bones).
Fix (no stock edits): a companion-provided **adopt operator** that
copies `rigforge_*` → `rigify_*` id-props on the active metarig/rig
before first stock-generate. Small, new-file-only, one-time per
.blend. New .blends need nothing.

## Companion proposal (sketch, pending owner go-ahead)

- New add-on `rigforge_companion` (GPL-2.0-or-later): the 19 new
  files + 5 panels verbatim, imports repointed `rigforge.*` →
  `rigify.*` (one function) / internal, own `metarigs/` dir + menu
  hook, own submodule list, plus the adopt operator. Requires stock
  Rigify enabled (clear error otherwise).
- Delete the forked `rigs/`, `utils/`, `generate.py`, `base_*`,
  `metarig_menu.py`, stock panels, stock operators (~85 files).
- Tests repoint `rigforge.*` → companion + `rigify.*` (mechanical);
  gates unchanged. Smoke test switches to stock generate + a
  companion-feature pass.
- Effort: ~1 focused session (mostly file moves + import rewrites);
  only design work is the presets menu hook + adopt operator.
- Payoff: Blender's Rigify fixes arrive for free; upstream-watch
  TODO item retires; tree shrinks ~80%.

## Risks / non-goals

- The adopt operator must be run once per old .blend (documented,
  undoable, refuses to double-run).
- If upstream ever renames `utils.bones.align_bone_z_axis`, the
  companion pins or vendors that one function (40 lines).
- Kept out of scope: any change to generated-rig content (proven
  identical — nothing to port).
