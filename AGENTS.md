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
- Remote: `github.com/chris-straka/rigforge` (owner flipping to
  PUBLIC, 2026-10-01; was private before). License file: `COPYING`
  (GPL-2.0 text; the tree is GPL-2.0-or-later per the SPDX headers).
  Nothing secret may enter this repo: no tokens, no game assets, no
  private paths beyond the owner's documented machine setup. Push
  rule: the 2026-10-01 standing push authorization was given for a
  private repo — ask before pushing until the owner re-confirms it
  for public. Never force-push or rewrite published history.
- Lint gate: `ruff check .` and `ruff format --check .` must be clean
  on files you touch (config: `ruff.toml`). The tree carries
  pre-existing lint debt (see TODO); don't bulk-fix outside your
  change, do leave your files clean.

## Siblings (separate repos, separate sessions)

- `~/SWE/retopoforge` — mesh stage (MIT engine + GPL extension).
  Strategy hub: `retopoforge/docs/rigging-strategy.md`. Read it first.
- `~/SWE/unirig-mac` — ML rigger port (MIT except vendored GPL
  michelangelo subtree; never call it MIT-clean). Optional: the
  bake-off (docs/bakeoff/final.md) went to rigforge on hero and
  creature quality, and this repo's own auto-placement covers
  unirig's zero-touch win. unirig survives only as an optional
  joint-hints source (coordinates in, never code).
- `~/Games/hll` — the game (Godot 4.7 + C#). Its chars are the test
  subjects; never commit game assets outside that repo.
- NOTE (2026-09-30): other agent sessions are active in this area
  (`retopoforge`, `SWE` workspaces). Check `git status`/`git log`
  before assuming a tree is yours alone.
