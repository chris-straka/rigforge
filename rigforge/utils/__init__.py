# SPDX-FileCopyrightText: 2019-2022 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

# These forwarding imports are for backwards compatibility with legacy code
# that expects a single utils.py file. New code should import directly from
# the modules that contain the utilities. Also, don't add more imports here.

from . import (
    bones,
    collections,
    errors,
    layers,
    misc,
    naming,
    rig,
    widgets,
    widgets_basic,
    widgets_special,
)
from .bones import _legacy_copy_bone as copy_bone
from .bones import _legacy_make_nonscaling_child as make_nonscaling_child
from .bones import (
    align_bone_roll,
    align_bone_x_axis,
    align_bone_y_axis,
    align_bone_z_axis,
    flip_bone,
    new_bone,
    put_bone,
)

# Definitions so bad as to make them strictly compatibility only
from .bones import copy_bone as copy_bone_simple
from .errors import MetarigError
from .layers import ControlLayersOption
from .misc import angle_on_plane, copy_attributes, gamma_correct, linsrgb_to_srgb
from .naming import (
    DEF_PREFIX,
    MCH_PREFIX,
    ORG_PREFIX,
    ROOT_NAME,
    deformer,
    insert_before_lr,
    make_deformer_name,
    make_mechanism_name,
    make_original_name,
    mch,
    org,
    org_name,
    random_id,
    strip_def,
    strip_mch,
    strip_org,
    strip_trailing_number,
    unique_name,
)
from .rig import (
    METARIG_DIR,
    RIG_DIR,
    TEMPLATE_DIR,
    connected_children_names,
    get_resource,
    has_connected_children,
    outdated_types,
    upgrade_metarig_types,
    write_metarig,
)
from .widgets import (
    WGT_PREFIX,
    create_circle_polygon,
    create_widget,
    obj_to_bone,
    write_widget,
)
from .widgets_basic import (
    create_bone_widget,
    create_chain_widget,
    create_circle_widget,
    create_cube_widget,
    create_limb_widget,
    create_line_widget,
    create_sphere_widget,
)
from .widgets_special import (
    create_compass_widget,
    create_neck_bend_widget,
    create_neck_tweak_widget,
    create_root_widget,
)
