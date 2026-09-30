# rigforge — agent notes

Fork of Blender's Rigify (0.6.10, copied from the Blender 5.2 app
bundle), module renamed `rigforge` to coexist with bundled Rigify.
Goal: game-ready rigs for HLL (Godot 4.7) characters and creatures —
beat upstream on auto-placement, skinning, presets, and export.

## Standing rules

- License is GPL-2.0-or-later (same as upstream). Never mix in
  MIT/permissive code with the intent to keep it permissive — this
  tree is GPL, like all Blender addons.
- The headless smoke test must stay green: `BLENDER_USER_SCRIPTS`
  overlay + enable + human metarig + generate (see README; canonical
  script at `/tmp/rigforge_smoke.py`, recreate if missing).
- Blender binary: `/Applications/Blender.app/Contents/MacOS/Blender`.
  Headless only unless the user approves a GUI launch.
- Remote: `github.com/chris-straka/rigforge` (private). Commit
  locally when asked; push only when asked.

## Siblings (separate repos, separate sessions)

- `~/SWE/retopoforge` — mesh stage (MIT engine + GPL extension).
  Strategy hub: `retopoforge/docs/rigging-strategy.md`. Read it first.
- `~/SWE/unirig-mac` — ML rigger port (MIT). Toolbox sibling, not a
  rival: this repo does heroes/control rigs, that one does creature
  volume/auto-placement. No contest, both stay.
- `~/Games/hll` — the game (Godot 4.7 + C#). Its chars are the test
  subjects; never commit game assets outside that repo.
- NOTE (2026-09-30): other agent sessions are active in this area
  (`retopoforge`, `SWE` workspaces). Check `git status`/`git log`
  before assuming a tree is yours alone.
