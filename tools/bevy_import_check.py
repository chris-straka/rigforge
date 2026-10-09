# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify rigforge GLBs load in Bevy (HLL's engine) headless.

Usage: tools/bevy_import_check.py <glb> [<glb> ...]

Runs glbkit's Bevy compatibility checker (rfcheck repo,
glbkit/compat/bevy: Bevy 0.19.1, the same pin as the game), which loads
each file through Bevy's own glTF loader with no window or GPU, spawns
the default scene and requires every declared skin to become SkinnedMesh
entities with joint indices and weights.

Checker binary: $BEVY_GLB_CHECK, else the build-tree default
~/Games/_blender/rfcheck/glbkit/compat/bevy/target/release/examples/check (build it
with `cargo build --release --example check` in that folder; Bevy is a
long first build).

Exit 0: every file loads with its skins. Exit 1: a file failed. Exit 2:
no checker binary (callers skip). Replaces the Godot import check, which
went away with the Godot version of HLL (2026-10-05).
"""

import os
import subprocess
import sys

DEFAULT = os.path.expanduser(
    "~/Games/_blender/rfcheck/glbkit/compat/bevy/target/release/examples/check"
)


def find_checker():
    candidate = os.environ.get("BEVY_GLB_CHECK", DEFAULT)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def main(paths):
    checker = find_checker()
    if checker is None:
        print("BEVY_SKIP: no Bevy checker binary (set BEVY_GLB_CHECK)")
        return 2
    proc = subprocess.run(
        [checker, *paths], capture_output=True, text=True, timeout=600
    )
    for line in proc.stdout.splitlines():
        # "ok   <path>: N meshes / ... / S SkinnedMesh entities, C clips"
        if line.startswith(("ok ", "FAIL ")):
            print("BEVY_STAT", line)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
