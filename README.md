# rigforge

Fork of Blender's Rigify (0.6.10, copied from the Blender 5.2 app bundle)
aimed at game-ready rigs: HLL (Godot 4.7) characters and creatures, rigged
in Blender, exported clean.

- License: GPL-2.0-or-later (same as upstream; Blender addons must be GPL).
- Upstream: none vendored as a remote (source is the Blender install);
  re-copy from a newer Blender to rebase.
- Day-0 state: pristine copy + headless smoke test (register, add human
  metarig, generate). See TODO.md.

Layout: `rigforge/` is the addon. Zip that folder for Install-from-Disk.

Smoke test:

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --factory-startup --python /tmp/rigforge_smoke.py
```
