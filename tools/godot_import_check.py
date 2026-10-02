#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify rigforge GLBs import into Godot headless (game-export depth).

Usage: tools/godot_import_check.py <glb> [<glb> ...]
Exit 0: all import with a skeleton. Exit 1: failure. Exit 2: Godot
not found (skip). Godot binary: $GODOT_BIN or the game-dev rig
default (/Applications/Godot.app).
"""

import os
import subprocess
import sys
import tempfile

GODOT_DEFAULT = "/Applications/Godot.app/Contents/MacOS/Godot"
GD_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "godot_import_check.gd"
)


def find_godot():
    candidate = os.environ.get("GODOT_BIN", GODOT_DEFAULT)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def main(paths):
    godot = find_godot()
    if godot is None:
        print("GODOT_SKIP: no Godot binary (set GODOT_BIN)")
        return 2
    # Godot needs a project context for full mesh data (without one,
    # skeletons load but surfaces come back empty). A throwaway dir
    # under /tmp keeps every repo clean; --path hangs, CWD works.
    project = tempfile.mkdtemp(prefix="rf_godot_check")
    with open(os.path.join(project, "project.godot"), "w") as fh:
        fh.write('config_version=5\n\n[application]\nconfig/name="rfcheck"\n')
    proc = subprocess.run(
        [godot, "--headless", "--script", GD_SCRIPT, "--quit-after", "1", "--", *paths],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=project,
    )
    stats = [ln for ln in proc.stdout.splitlines() if "GODOT_STAT" in ln]
    fails = [ln for ln in proc.stdout.splitlines() if "LOAD_FAIL" in ln]
    print("\n".join(stats + fails))
    if proc.returncode != 0 or fails or len(stats) != len(paths):
        if proc.returncode != 0:
            print(proc.stderr[-2000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
