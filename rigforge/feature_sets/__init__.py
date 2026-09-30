# SPDX-FileCopyrightText: 2010-2022 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

# Redirect the module loader to the user scripts directory.

# Thus feature set modules can be added to this package without
# writing to the actual Rigforge installation directory.


def _install_path():
    import os

    import bpy

    return os.path.join(bpy.utils.script_path_user(), "rigforge")


__path__ = [_install_path()]
