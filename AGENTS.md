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
  script is `tests/smoke.py`, also run by CI on every PR).
- Blender binary: `/Applications/Blender.app/Contents/MacOS/Blender`.
  Headless only unless the user approves a GUI launch.
- Remote: `github.com/chris-straka/rigforge` (owner flipping to
  PUBLIC, 2026-10-01; was private before). License file: `COPYING`
  (GPL-2.0 text; the tree is GPL-2.0-or-later per the SPDX headers).
  Nothing secret may enter this repo: no tokens, no game assets, no
  private paths beyond the owner's documented machine setup. Commit
  and push to `origin/main` on your own after each completed chunk of
  work; don't wait for approval (standing authorization from the
  owner, 2026-10-01, re-confirmed for public). Never force-push or
  rewrite published history.
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
